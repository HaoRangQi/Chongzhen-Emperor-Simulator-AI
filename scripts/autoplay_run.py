#!/usr/bin/env python3
"""AI 皇帝无头跑局：不依赖网页，直接在终端观察 AI 托管全过程。

真实 LLM 跑局（需先 export OPENAI_API_KEY 等）：
    python3 scripts/autoplay_run.py --goal "10月内剪除魏忠贤+辽东欠饷≤30" --turns 10

无 API key 冒烟（--mock：stub agent 驱动真实工具链，验证事件总线/工具/护栏）：
    python3 scripts/autoplay_run.py --mock --turns 2

事件流会同时打印到终端；--transcript 可另存 jsonl 复盘。
 Ctrl+C 随时停止（等价于网页上的「停止/接管」，存档不坏）。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Ev:
    """agno 流式事件的极简仿制品：.event + .content。"""

    def __init__(self, event: str, content: str) -> None:
        self.event = event
        self.content = content


class RunOutput:
    """agno RunOutput 的极简仿制品（type 名须为 RunOutput，driver 靠它收尾）。"""

    metrics = None
    messages: list = []


class _StubAgent:
    """mock 模式的“AI 皇帝”：不调 LLM，按固定脚本驱动真实工具链。"""

    def __init__(self, driver) -> None:
        self._driver = driver

    def run(self, prompt: str, **kwargs):
        tools = {f.__name__: f for f in self._driver._tools}
        # 脚本：看局 → 名册 → 召见（无 LLM 会走失败兜底）→ 亲拟草案 → 颁诏
        try:
            tools["get_situation"]()
            tools["list_ministers"]()
            tools["chat_with_minister"]("毕自严", "国库还有多少银子？辽东欠饷如何？")
            tools["add_draft"](
                "奉天承运皇帝诏曰：户部即核辽东军饷亏欠之数，于内库拨银补发，"
                "敢有侵欺者，着锦衣卫逮问。钦此。"
            )
            tools["list_drafts"]()
            tools["issue_decree"]()
        except Exception as e:  # noqa: BLE001
            print(f"[stub] 工具链异常：{e}", flush=True)
        yield _Ev("RunContent", f"[mock] 本月脚本执行完毕（{len(prompt)} 字提示）")
        yield RunOutput()


def _print_event(event: dict) -> None:
    kind = event.get("kind", "")
    data = event.get("data") or {}
    turn = event.get("turn", 0)
    if kind == "turn_start":
        print(f"\n=== 第{turn}回合 {data.get('year')}年{data.get('period')}月 开局 ===", flush=True)
    elif kind == "thought_delta":
        sys.stdout.write(str(data.get("content", "")))
        sys.stdout.flush()
    elif kind == "thought_done":
        print(flush=True)
    elif kind == "tool_call":
        print(f"  [工具] {data.get('tool')} {data.get('args') or ''}", flush=True)
    elif kind == "tool_result":
        print(f"  [结果] {data.get('summary', '')}", flush=True)
    elif kind == "chat_start":
        print(f"  [召见] {data.get('minister')}：{data.get('message', '')[:60]}", flush=True)
    elif kind == "chat_delta":
        sys.stdout.write(str(data.get("content", "")))
        sys.stdout.flush()
    elif kind == "chat_done":
        print(flush=True)
    elif kind == "draft":
        print(f"  [草案] {data.get('action')} #{data.get('id')}", flush=True)
    elif kind == "issued":
        print(f"  [颁诏] {str(data.get('decree', ''))[:80]}……", flush=True)
    elif kind == "decision":
        print(f"  [决策点] {len(data.get('decisions') or [])} 项待裁", flush=True)
    elif kind == "report":
        print(f"  [战报] {str(data.get('report', ''))[:200]}……", flush=True)
    elif kind == "status":
        print(f"  [状态] {data.get('status')} {data.get('reason', '')}", flush=True)
    elif kind == "error":
        print(f"  [错误] {data.get('message', '')}", flush=True)
    elif kind == "finished":
        print(f"\n=== 托管结束：{data.get('reason', '')}（共 {data.get('turns_played')} 月）===", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 皇帝无头跑局")
    parser.add_argument("--goal", default="", help="本局目标")
    parser.add_argument("--persona", default="balanced",
                        choices=["balanced", "suspicious", "hardline", "merciful", "schemer", "reckless", "doomer"])
    parser.add_argument("--turns", type=int, default=10, help="最多跑几个月（0=不限）")
    parser.add_argument("--steps", type=int, default=4, help="每月 agent 步数预算")
    parser.add_argument("--delay", type=float, default=0.5, help="步间延时（秒）")
    parser.add_argument("--db", default="", help="存档路径（默认临时新库，跑完即弃）")
    parser.add_argument("--mock", action="store_true", help="无 LLM 冒烟模式")
    parser.add_argument("--transcript", action="store_true", help="事件流另存 jsonl")
    args = parser.parse_args()

    if args.db:
        os.environ["MING_SIM_DB"] = args.db
    else:
        os.environ["MING_SIM_DB"] = str(Path(tempfile.mkdtemp(prefix="ming_autoplay_")) / "ming_sim.db")
    os.environ.setdefault("MING_SIM_SEED", "7")
    if args.mock:
        # mock 模式不建真 agent；给个占位 key 让 WebGame 能构造（工具链里真实 LLM
        # 调用的失败会被工具自身兜底成错误文本，不影响冒烟）。
        os.environ.setdefault("OPENAI_API_KEY", "mock-key-not-used")
        os.environ.setdefault("OPENAI_BASE_URL", "https://api.openai.com/v1")
        os.environ.setdefault("OPENAI_MODEL", "mock-model")
        os.environ.setdefault("OPENAI_TIMEOUT_SECONDS", "10")
        os.environ.setdefault("OPENAI_CONNECT_TIMEOUT_SECONDS", "5")
        os.environ.setdefault("OPENAI_READ_TIMEOUT_SECONDS", "5")

    from ming_sim.autoplay import AutoplayConfig, AutoplayDriver
    from ming_sim.autoplay.tools import build_tools

    if args.mock:
        # GameSession 构造时会 ping 一次 LLM；mock 模式直接短路连通性检查。
        import ming_sim.session as _session_mod
        _session_mod.verify_llm_available = lambda cfg: None

    from web_app import WebGame

    game = WebGame(fresh=not bool(args.db))
    config = AutoplayConfig(
        goal=args.goal,
        persona=args.persona,
        max_turns=max(0, args.turns),
        max_steps_per_turn=max(1, args.steps),
        step_delay=max(0.0, args.delay),
    )
    driver = AutoplayDriver(game, config)
    if args.transcript:
        driver._transcript_path = os.environ["MING_SIM_DB"] + ".autoplay.jsonl"
    if args.mock:
        # 不建真 agent；_tools 挂上真实工具链，stub 按脚本调用
        driver._tools = build_tools(driver)
        driver._build_agent = lambda: _StubAgent(driver)  # type: ignore[assignment]

    sub = driver.subscribe()
    driver.start()

    print(f"AI 崇祯已升座（人格：{args.persona}，目标：{args.goal or '挽狂澜于既倒'}）", flush=True)
    print("Ctrl+C 停止（存档安全，可从网页/CLI 接着玩）\n", flush=True)
    last_status = ""
    try:
        while True:
            try:
                while True:
                    event = sub.get_nowait()
                    _print_event(event)
            except Exception:
                pass
            payload = driver.status_payload()
            tag = f"{payload['status']}|{payload['turn']}|{payload['steps_this_turn']}|{payload['tool_calls']}"
            if tag != last_status:
                last_status = tag
            if payload["status"] in ("finished", "stopped", "error", "paused"):
                time.sleep(0.3)
                try:
                    while True:
                        _print_event(sub.get_nowait())
                except Exception:
                    pass
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止……", flush=True)
        driver.stop("Ctrl+C 停止")
        for _ in range(50):
            if not driver.is_active():
                break
            time.sleep(0.2)

    payload = driver.status_payload()
    print(
        f"\n退出：status={payload['status']} reason={payload['reason']} "
        f"turns={payload['turns_played']} tools={payload['tool_calls']} chats={payload['chat_rounds']}",
        flush=True,
    )
    print(f"存档：{os.environ['MING_SIM_DB']}（--db 指定它即可从同一进度续跑）", flush=True)
    return 0 if payload["status"] in ("finished", "stopped", "paused") else 1


if __name__ == "__main__":
    raise SystemExit(main())
