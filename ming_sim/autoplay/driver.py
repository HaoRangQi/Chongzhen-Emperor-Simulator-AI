"""AI 皇帝驱动器：后台线程驱动 GameSession 自动游玩，事件实时外发供观战。L8。

核心约定：
- 单一写入者：运行期只有 driver 线程操作 GameSession；web 层对写接口加 409 锁。
- 步间暂停：暂停/停止/护栏都在安全点（两次 agent.run 之间）生效；
  agent.run 与每个工具调用是原子的，不会把存档写坏。接管 = 停止，人从同一进度接着玩。
- 观战回放：subscribe() 注册即先重放最近 _HISTORY_LIMIT 条事件，晚进来的观众不掉队。
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional

from agno.agent import Agent

from ming_sim.agents import _ctx, _dump_llm_messages, create_chat_model
from ming_sim.exceptions import LLMUnavailable
from ming_sim.llm_config import for_role
from ming_sim.models import LLMConfig
from ming_sim.token_stats import record_stream_metrics

from .events import (
    KIND_ERROR,
    KIND_FINISHED,
    KIND_ISSUED,
    KIND_REPORT,
    KIND_STATUS,
    KIND_THOUGHT_DELTA,
    KIND_THOUGHT_DONE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KIND_TURN_START,
    make_event,
)
from .prompt import PERSONA_LABELS, build_instructions
from .tools import build_tools, situation_text, snap_metrics

_HISTORY_LIMIT = 500

# 观战展示语言纪律：圣心独白只上屏含汉字的内容（模型英文思考链/工具前导一律过滤）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class _StopRequested(Exception):
    """驱动器线程收到停止请求，在安全点抛出以退出主循环。"""


def emperor_llm_config(base: LLMConfig) -> LLMConfig:
    """皇帝 Agent 用的 LLM 配置：默认跟游戏主模型一致（低成本长线），
    可用 MING_SIM_EMPEROR_MODEL / _BASE_URL / _API_KEY 覆盖（想换更强模型时）。"""
    model = os.environ.get("MING_SIM_EMPEROR_MODEL", "").strip()
    base_url = os.environ.get("MING_SIM_EMPEROR_BASE_URL", "").strip()
    api_key = os.environ.get("MING_SIM_EMPEROR_API_KEY", "").strip()
    if not (model or base_url or api_key):
        return base
    return LLMConfig(
        api_key=api_key or base.api_key,
        base_url=base_url or base.base_url,
        model=model or base.model,
        max_tokens=base.max_tokens,
        timeout_seconds=base.timeout_seconds,
        connect_timeout_seconds=base.connect_timeout_seconds,
        read_timeout_seconds=base.read_timeout_seconds,
        thinking_level=base.thinking_level,
        advanced_model=base.advanced_model,
        advanced_base_url=base.advanced_base_url,
        advanced_api_key=base.advanced_api_key,
        advanced_thinking_level=base.advanced_thinking_level,
    )


@dataclass
class AutoplayConfig:
    """托管参数。goal=本局目标；persona=人格预设键（balanced/suspicious/hardline/
    merciful/schemer/reckless/doomer）。0/空 = 不限。"""

    goal: str = ""
    persona: str = "balanced"
    max_turns: int = 0
    max_steps_per_turn: int = 4
    max_consec_errors: int = 3
    error_backoff: float = 5.0
    token_budget: int = 0
    step_delay: float = 0.0
    temperature: float = 0.75
    llm_config: Optional[LLMConfig] = None


class AutoplayDriver:
    """驱动 WebGame 里的崇祯 AI 自动游玩，并向外发观战事件。"""

    def __init__(self, game: Any, config: Optional[AutoplayConfig] = None) -> None:
        self.game = game
        self.config = config or AutoplayConfig()
        self.status: str = "idle"  # idle|running|paused|stopped|finished|error
        self._thread: Optional[threading.Thread] = None
        self._pause_gate = threading.Event()
        self._pause_gate.set()
        self._pending_pause = False
        self._pending_stop = False
        self._park_reason = ""
        self._finished_reason = ""
        self._last_error = ""
        # 计数
        self._turns_played = 0
        self._steps_this_turn = 0
        self._issued_this_turn = False
        self._tool_calls = 0
        self._chat_rounds = 0
        self._consec_errors = 0
        self._tokens: Dict[str, int] = {"input": 0, "output": 0, "total": 0}
        self._started_at: Optional[float] = None
        self._month_start_metrics: Dict[str, float] = {}
        # 观战
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self._history: Deque[Dict[str, Any]] = deque(maxlen=_HISTORY_LIMIT)
        self._lock = threading.Lock()
        self._agent: Optional[Agent] = None
        self._tools: List[Any] = []
        self._transcript_path = ""
        self._need_begin_month = True

    # ── 生命周期 ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._pending_stop = False
        self._pending_pause = False
        self._pause_gate.set()
        self._started_at = time.time()
        self.status = "running"
        self._thread = threading.Thread(target=self._run, name="autoplay-driver", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self.status not in ("running",):
            return
        self._pending_pause = True
        self._park_reason = "人类观察员手动暂停"
        self._pause_gate.clear()

    def resume(self) -> None:
        # 只要还活着（含"暂停请求已发出但线程还没驻留"的窗口期）都允许恢复：
        # 清掉 pending 标志并放行 gate，避免 pause→resume 快速连点后仍驻留。
        if self.status not in ("paused", "running"):
            return
        self._pending_pause = False
        self._park_reason = ""
        self._pause_gate.set()

    def stop(self, reason: str = "已停止 AI 托管") -> None:
        self._pending_stop = True
        self._finished_reason = reason
        self._pause_gate.set()  # 让停在 gate 上的线程立即醒来退出

    def is_active(self) -> bool:
        """是否占用游戏写锁（运行中/暂停中/正在停止都算）。"""
        return self.status in ("running", "paused") or (
            self._pending_stop and self._thread is not None and self._thread.is_alive()
        )

    def join(self, timeout: float = 0.0) -> None:
        if self._thread is not None and timeout > 0:
            self._thread.join(timeout=timeout)

    # ── 事件总线 ────────────────────────────────────────────────────────
    def emit(self, kind: str, data: Optional[Dict[str, Any]] = None) -> None:
        try:
            turn = int(self.game.state.turn)
        except Exception:
            turn = 0
        event = make_event(kind, turn, data)
        with self._lock:
            self._history.append(event)
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(event)
            except queue.Full:
                pass
        if self._transcript_path:
            try:
                with open(self._transcript_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        sub: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(sub)
            replay = list(self._history)
        for event in replay:
            try:
                sub.put_nowait(event)
            except queue.Full:
                break
        return sub

    def unsubscribe(self, sub: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    # ── 状态查询 ────────────────────────────────────────────────────────
    def status_payload(self) -> Dict[str, Any]:
        snap = snap_metrics(self.game)
        persona = self.config.persona
        return {
            "active": self.is_active(),
            "status": self.status,
            "goal": self.config.goal,
            "persona": persona,
            "persona_label": PERSONA_LABELS.get(persona, persona),
            "turn": snap["turn"],
            "year": snap["year"],
            "period": snap["period"],
            "phase": snap["phase"],
            "ended": snap["ended"],
            "metrics": snap["metrics"],
            "turns_played": self._turns_played,
            "steps_this_turn": self._steps_this_turn,
            "tool_calls": self._tool_calls,
            "chat_rounds": self._chat_rounds,
            "tokens": dict(self._tokens),
            "elapsed_sec": round(time.time() - self._started_at, 1) if self._started_at else 0.0,
            "reason": self._park_reason or self._finished_reason or self._last_error,
            "stop_requested": self._pending_stop,
            "error_streak": self._consec_errors,
        }

    # ── 内部：工具计数器（tools.py 回调进来） ───────────────────────────
    def _note_tool_call(self, tool: str, args: Dict[str, Any]) -> None:
        self._tool_calls += 1
        self.emit(KIND_TOOL_CALL, {"tool": tool, "args": args})

    def _note_tool_result(self, tool: str, summary: str) -> None:
        self.emit(KIND_TOOL_RESULT, {"tool": tool, "summary": summary[:300]})

    def _mark_turn_issued(self, report: str, decree: str) -> None:
        """工具层上报本回合已颁诏：发 issued/report 事件，做月度收尾计数。"""
        self._issued_this_turn = True
        snap_after = snap_metrics(self.game)
        if decree:
            self.emit(KIND_ISSUED, {"decree": decree, "turn": snap_after["turn"]})
        self.emit(
            KIND_REPORT,
            {
                "report": report,
                "month_start_metrics": dict(self._month_start_metrics),
                "metrics_after": snap_after["metrics"],
                "year": snap_after["year"],
                "period": snap_after["period"],
                "turn": snap_after["turn"],
            },
        )

    # ── 内部：护栏与状态迁移 ────────────────────────────────────────────
    def _park(self, reason: str) -> None:
        """请求暂停（当前 agent.run 结束后生效）。"""
        self._park_reason = reason
        self._pending_pause = True
        self._pause_gate.clear()
        self.emit(KIND_STATUS, {"status": self.status, "park_requested": True, "reason": reason})

    def _fail(self, message: str) -> None:
        self._last_error = message
        self.status = "error"
        self.emit(KIND_ERROR, {"message": message})
        self.emit(KIND_STATUS, {"status": "error", "reason": message})

    def _finish(self, reason: str) -> None:
        self._finished_reason = reason
        self.status = "finished"
        self.emit(KIND_FINISHED, {"reason": reason, "turns_played": self._turns_played})
        self.emit(KIND_STATUS, {"status": "finished", "reason": reason})

    def _safe_gate(self) -> None:
        """安全点：暂停则驻留等恢复；收到停止则抛 _StopRequested。"""
        if self._pending_stop:
            raise _StopRequested()
        if self._pending_pause:
            self.status = "paused"
            self.emit(KIND_STATUS, {"status": "paused", "reason": self._park_reason or "已暂停"})
            self._pause_gate.wait()
            if self._pending_stop:
                raise _StopRequested()
            self.status = "running"
            # 恢复后给一轮新的步数预算（不重开新月：turn_start 只在真正跨月时发）
            self._steps_this_turn = 0
            self.emit(KIND_STATUS, {"status": "running", "reason": "继续托管"})

    # ── 内部：主循环 ────────────────────────────────────────────────────
    def _run(self) -> None:
        cfg = self.config
        try:
            self._agent = self._build_agent()
        except Exception as e:  # noqa: BLE001
            self._fail(f"AI 皇帝初始化失败：{e}")
            return
        self.emit(KIND_STATUS, {
            "status": "running",
            "reason": "AI 皇帝已升座",
            "goal": cfg.goal,
            "persona": cfg.persona,
        })
        while True:
            try:
                self._safe_gate()
                state = self.game.state
                if state.ended:
                    self._finish(f"大局已定（{state.ending_status or '结局'}），托管结束。")
                    return
                if cfg.max_turns and self._turns_played >= cfg.max_turns:
                    self._finish(f"已达回合上限 {cfg.max_turns}，托管结束。")
                    return
                if cfg.token_budget and self._tokens.get("total", 0) >= cfg.token_budget:
                    self._park(f"token 预算 {cfg.token_budget} 已用尽。")
                    continue
                if self._need_begin_month:
                    self._begin_month()
                self._steps_this_turn += 1
                first = self._steps_this_turn == 1
                prompt = self._month_prompt(first=first)
                self._run_agent_stream(prompt)
                self._consec_errors = 0
                if self._issued_this_turn:
                    # 本月已颁诏：重置步数并在下一轮开新月
                    self._steps_this_turn = 0
                    self._issued_this_turn = False
                    self._need_begin_month = True
                    self.emit(KIND_STATUS, {
                        "status": "running",
                        "month_done": True,
                        "metrics": snap_metrics(self.game)["metrics"],
                    })
                    continue
                if self._steps_this_turn >= max(1, cfg.max_steps_per_turn):
                    self._park(
                        f"第 {self.game.state.turn} 回合步数达上限 "
                        f"({max(1, cfg.max_steps_per_turn)}) 且未颁诏。"
                    )
                    continue
            except _StopRequested:
                break
            except LLMUnavailable as e:
                self._handle_step_error(f"LLM 调用失败：{e}")
            except Exception as e:  # noqa: BLE001
                self._handle_step_error(f"本月处理异常：{e}")
        # 线程退出
        if self.status not in ("finished", "error"):
            self.status = "stopped"
            self.emit(KIND_STATUS, {"status": "stopped", "reason": self._finished_reason or "已停止"})

    def _handle_step_error(self, message: str) -> None:
        cfg = self.config
        self._consec_errors += 1
        self.emit(KIND_ERROR, {"message": message, "error_streak": self._consec_errors})
        if self._consec_errors >= max(1, cfg.max_consec_errors):
            self._park(f"连续 {self._consec_errors} 次出错，已暂停等待人工处理。")
            return
        time.sleep(min(30.0, cfg.error_backoff * self._consec_errors))

    def _begin_month(self) -> None:
        self._issued_this_turn = False
        self._need_begin_month = False
        self._turns_played += 1
        self._month_start_metrics = snap_metrics(self.game)["metrics"]
        try:
            situation = situation_text(self.game, self.config.goal)
        except Exception:
            situation = ""
        self.emit(KIND_TURN_START, {
            "year": int(self.game.state.year),
            "period": int(self.game.state.period),
            "turn": int(self.game.state.turn),
            "goal": self.config.goal,
            "metrics": dict(self._month_start_metrics),
            "situation": situation,
        })

    def _month_prompt(self, first: bool) -> str:
        try:
            situation = situation_text(self.game, self.config.goal)
        except Exception as e:  # noqa: BLE001
            situation = f"（局势读取失败：{e}）"
        if first:
            return (
                f"【新的一月】\n{situation}\n\n"
                "请先看局，判断本月最急之处；再决定召见谁、问什么。"
                "与大臣议定方略后让其拟旨；本月须至少颁诏一次才能进入下一月。"
            )
        return (
            f"【继续本月】尚未颁诏。\n{situation}\n\n"
            "请继续：或继续召见议事、核定大臣拟旨；或审定草案后 issue_decree 颁诏。"
        )

    def _build_agent(self) -> Agent:
        base_cfg = self.config.llm_config or self.game.session.llm_config
        llm_cfg = emperor_llm_config(base_cfg)
        try:
            campaign_id = str(self.game.db.kv_get("campaign_id") or "").strip()
        except Exception:
            campaign_id = ""
        model = create_chat_model(
            llm_cfg,
            temperature=float(self.config.temperature),
            max_tokens=max(4000, int(llm_cfg.max_tokens or 0)),
            enable_thinking=False,
        )
        tools = build_tools(self)
        self._tools = tools
        extra_instructions: List[str] = []
        try:
            world_prompt = getattr(_ctx(), "game_world_prompt", "")
            if world_prompt:
                extra_instructions.append(str(world_prompt))
        except Exception:
            pass
        agent = Agent(
            name="AI 崇祯",
            id="emperor-autoplay",
            session_id=f"emperor-autoplay-{campaign_id or 'default'}",
            db=getattr(self.game.session, "agno_db", None),
            model=model,
            instructions=extra_instructions + [build_instructions(self.config.goal, self.config.persona)],
            tools=tools,
            add_history_to_context=True,
            num_history_runs=6,
            tool_call_limit=12,
            markdown=False,
        )
        return agent

    def _run_agent_stream(self, prompt: str) -> None:
        """跑一步 agent：thinking 增量转 thought_delta 事件，跑完记账/落盘。

        语言纪律（agno 3.x 事件模型）：
        - `reasoning_content` 是模型内部思考链——reasoner 类模型常用英文思考，
          这不是皇帝的「圣心独白」，一律不上屏；
        - 工具调用前导文本/premable 也可能是英文——可见内容若整段无汉字，
          同样不上屏（圣心独白按提示词必须是中文）；
        两处都只过滤观战展示，不影响任何游戏状态。"""
        agent = self._agent
        if agent is None:
            raise RuntimeError("AI 皇帝尚未初始化。")
        stream = agent.run(
            prompt, stream=True, stream_events=True, yield_run_output=True,
        )
        thought_parts: List[str] = []
        run_output = None
        for event in stream:
            try:
                event_name = getattr(event, "event", "")
            except Exception:
                event_name = ""
            # 1) 思考链（reasoning）：agno 3.x 可能走独立事件，也可能搭 RunContent 便车
            reasoning = getattr(event, "reasoning_content", None)
            content = getattr(event, "content", None)
            if isinstance(reasoning, str) and reasoning and not (isinstance(content, str) and content):
                continue
            if event_name == "RunContent" and isinstance(content, str) and content:
                piece = content
                # 2) 非中文内容（英文思考漏出/工具前导）不上屏
                if not _CJK_RE.search(piece):
                    continue
                thought_parts.append(piece)
                self.emit(KIND_THOUGHT_DELTA, {"content": piece})
            if type(event).__name__ in ("RunOutput", "RunCompletedEvent"):
                run_output = event
        text = "".join(thought_parts).strip()
        if text:
            self.emit(KIND_THOUGHT_DONE, {"content": text[:2000]})
        # token 记账（agno metrics 口径，含 cache_read/write）
        try:
            metrics = getattr(run_output, "metrics", None)
            model_id = getattr(getattr(agent, "model", None), "id", None) or "emperor"
            record_stream_metrics(str(model_id), metrics, caller_tag="AI皇帝托管")
            step_total = int(getattr(metrics, "total_tokens", 0) or 0)
            self._tokens["input"] += int(getattr(metrics, "input_tokens", 0) or 0)
            self._tokens["output"] += int(getattr(metrics, "output_tokens", 0) or 0)
            self._tokens["total"] += step_total
        except Exception:
            pass
        try:
            _dump_llm_messages(run_output, "AI皇帝托管", agent=agent)
        except Exception:
            pass
        if self.config.step_delay > 0:
            time.sleep(self.config.step_delay)
