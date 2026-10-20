"""皇帝 Agent 的结构化工具：直接调 GameSession，与人类走同一条操作通道。L8。

每个工具都是 driver 上的闭包：入口 emit tool_call / tool_result 事件，
退出把结果压缩成文本喂回 agent——agent 看到的世界和网页玩家完全一致，
只是它用 mouse 的时间省下来想正事去了。

安全约定：所有工具都在 driver 线程内被调用（agent.run 同步跑在该线程），
与 web 层的单一写入者锁共同保证同一时刻只有一个写者。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ming_sim.assets import format_money
from ming_sim.exceptions import LLMUnavailable

from .events import (
    KIND_CHAT_DELTA,
    KIND_CHAT_DONE,
    KIND_CHAT_START,
    KIND_DECISION,
    KIND_DRAFT,
    KIND_TOOL_CALL,
)

CALLS_PER_MONTH_HINT = "本月须至少 issue_decree 一次才能进入下一月。"


def _trim(text: object, limit: int) -> str:
    t = str(text or "").strip()
    return t if len(t) <= limit else t[:limit] + "……（截断）"


def _metrics_line(game: Any) -> str:
    m = dict(getattr(game.state, "metrics", {}) or {})
    return (
        f"【结算后国力】国库 {format_money(m.get('国库', 0))}｜内库 {format_money(m.get('内库', 0))}"
        f"｜民心 {int(float(m.get('民心', 0)))}｜皇威 {int(float(m.get('皇威', 0)))}"
    )


def situation_text(game: Any, goal: str = "") -> str:
    """本月朝局全貌（get_situation 工具 + 驱动器月度提示词共用）。"""
    session = game.session
    state = game.state
    lines: List[str] = []
    lines.append(f"【时辰】{state.year}年{state.period}月 · 第{state.turn}回合 · 阶段 {state.turn_phase}")
    m = dict(state.metrics)
    lines.append(
        f"【国力】国库 {format_money(m.get('国库', 0))}｜内库 {format_money(m.get('内库', 0))}"
        f"｜民心 {int(float(m.get('民心', 0)))}｜皇威 {int(float(m.get('皇威', 0)))}"
    )
    try:
        lines.append("【钱粮账目】\n" + _trim(game.db.treasury_report(state, limit=6), 600))
    except Exception:
        pass
    try:
        issues = game.issue_payloads()
        issues.sort(key=lambda x: int(x.get("severity", 0) or 0), reverse=True)
        if issues:
            rows = [
                f"- {i.get('title', '?')}｜{i.get('stage_text') or i.get('phase', '')}"
                f"｜进度 {i.get('bar_value', 0)}｜严重度 {i.get('severity', 0)}"
                f"｜承办：{i.get('assignee') or '无'}"
                for i in issues[:8]
            ]
            lines.append("【在办事项】(按严重度前 8)\n" + "\n".join(rows))
    except Exception:
        pass
    for label, key in (("【军情预警】", "army_warning"), ("【地方预警】", "region_warning")):
        try:
            if key == "army_warning":
                text = game.db.army_report(limit=5)
            else:
                text = game.db.region_report(limit=5)
            if text:
                lines.append(label + "\n" + _trim(text, 400))
        except Exception:
            pass
    summary = _trim(getattr(session, "previous_summary", "") or "", 900)
    if summary:
        lines.append("【上月奏报】\n" + summary)
    if goal:
        lines.append(f"【本局目标】{goal}")
    return "\n".join(lines)


def build_tools(driver: Any) -> list:
    """构造皇帝 Agent 工具集。driver 需提供 emit() 与 game。"""
    game = driver.game
    session = game.session

    # ── 看局 ────────────────────────────────────────────────────────────
    def get_situation() -> str:
        """读取本月朝局全貌：国力四项、钱粮账目、在办事项、军队与地方预警、上月奏报。每月开局先调用。"""
        driver._note_tool_call("get_situation", {})
        try:
            text = situation_text(game, driver.config.goal)
            driver._note_tool_result("get_situation", text)
            return text
        except Exception as e:  # noqa: BLE001
            return f"（看局失败：{e}）"

    def list_ministers() -> str:
        """在朝大臣名册：姓名/职位/类别/派系/状态。召见前先查，避免写错名字。"""
        driver._note_tool_call("list_ministers", {})
        try:
            views = session.list_ministers()
        except Exception as e:  # noqa: BLE001
            return f"（名册读取失败：{e}）"
        if not views:
            return "（在朝无人？）"
        rows = [
            f"- {v.name}｜{v.office}｜{v.office_type}｜{v.faction}｜{v.status}"
            for v in views
        ]
        driver._note_tool_result("list_ministers", f"{len(views)} 位在朝：" + "、".join(v.name for v in views))
        return "\n".join(rows) + "\n（仅 status=active 可召见。）"

    def chat_with_minister(minister_name: str, message: str) -> str:
        """召见一位大臣并对话。问政、查账、试探、问责都可以；想让大臣拟旨，就在对话里以皇帝口吻示意采纳（如"准奏"），大臣会自动拟旨入草案。"""
        name = str(minister_name or "").strip()
        msg = str(message or "").strip()
        driver._note_tool_call("chat_with_minister", {"minister": name, "message": _trim(msg, 80)})
        cap = max(1, int(getattr(driver.config, "max_chats_per_turn", 6) or 6))
        if driver._chats_this_turn >= cap:
            return (
                f"（本月召见次数已用完（{cap} 次）。不要再召见任何人——"
                "审阅现有草案后直接 issue_decree 颁诏；若草案不合意，用 add_draft 自己写一道再颁诏。）"
            )
        if not name or not msg:
            return "（参数不全：需要大臣姓名与问话内容。先用 list_ministers() 查名册。）"
        try:
            known = {v.name for v in session.list_ministers()}
        except Exception:
            known = set()
        if name not in known and name not in getattr(session, "temporary_characters", {}) \
                and name not in getattr(game, "content", {}).characters:
            return f"（未查到大臣「{name}」。在朝名册见 list_ministers() 输出。）"
        driver.emit(KIND_CHAT_START, {"minister": name, "message": msg})
        deltas: List[str] = []
        payload: Dict[str, Any] = {}
        err = ""
        try:
            for item in game.chat_stream(name, msg):
                kind = str(item.get("type", ""))
                if kind == "delta":
                    piece = str(item.get("content", ""))
                    deltas.append(piece)
                    driver.emit(KIND_CHAT_DELTA, {"minister": name, "content": piece})
                elif kind == "done":
                    payload = item.get("payload") or {}
                elif kind == "error":
                    err = str(item.get("detail") or item.get("message") or "召见失败")
        except Exception as e:  # noqa: BLE001
            err = str(e)
        if err and not payload:
            driver.emit(KIND_CHAT_DONE, {"minister": name, "error": err})
            return f"（召见失败：{err}）"
        answer = "".join(deltas).strip() or str(payload.get("answer", "")).strip()
        lines = [f"【{name}奏对】{answer}"]
        court_action = str(payload.get("court_action") or "")
        if court_action == "summon" and payload.get("next_minister"):
            lines.append(f"（大臣奏请引见：{payload['next_minister']}，可直接 chat_with_minister 召见。）")
        elif court_action == "dismiss":
            lines.append("（大臣请求退下。可继续召见他人，或收尾颁诏。）")
        proposed = payload.get("proposed_directive")
        if proposed:
            lines.append(
                f"【大臣拟旨待核定】#{proposed.get('id')}（status={proposed.get('status')}）："
                f"{_trim(proposed.get('text', ''), 200)}"
                "用 confirm_draft(id) 准奏入档，或 reject_draft(id) 驳回让其重拟。"
            )
        if payload.get("secret_order_id"):
            lines.append(f"（已下密令 #{payload['secret_order_id']}，按月暗中推进，办结可得实证。）")
        if payload.get("tax_adjusted"):
            lines.append(f"（户部已调税立项：{payload['tax_adjusted']}）")
        directives = payload.get("directives") or []
        if directives:
            lines.append(
                "【当前草案】"
                + "；".join(
                    f"#{d.get('id')}[{d.get('status')}]{_trim(d.get('text', ''), 40)}"
                    for d in directives
                )
            )
        suggestions = payload.get("suggestions") or []
        labels = [str(s.get("label", "")) for s in suggestions if s.get("label")]
        if labels:
            lines.append("【可继续进言】" + "、".join(labels[:6]))
        driver._chat_rounds += 1
        driver._chats_this_turn += 1
        driver.emit(
            KIND_CHAT_DONE,
            {"minister": name, "answer": _trim(answer, 500), "proposed": proposed},
        )
        driver._note_tool_result("chat_with_minister", f"{name}：{_trim(answer, 120)}")
        return "\n".join(lines)

    # ── 草案管理 ────────────────────────────────────────────────────────
    def list_drafts() -> str:
        """本月已入档的诏令草案（含大臣拟旨待核定的 pending）。"""
        driver._note_tool_call("list_drafts", {})
        try:
            drafts = session.list_directives()
        except Exception as e:  # noqa: BLE001
            return f"（草案读取失败：{e}）"
        if not drafts:
            return "（本月暂无草案。）" + CALLS_PER_MONTH_HINT
        body = "\n".join(
            f"#{d.id} [{d.status}]（{d.source or '—'}）{_trim(d.text, 120)}" for d in drafts
        )
        driver._note_tool_result("list_drafts", f"{len(drafts)} 条草案")
        return body

    def confirm_draft(draft_id: int) -> str:
        """准奏：把大臣拟旨的 pending 草案确认入档（status→draft）。"""
        driver._note_tool_call("confirm_draft", {"id": draft_id})
        try:
            session.confirm_directive(int(draft_id))
        except Exception as e:  # noqa: BLE001
            return f"（准奏失败：{e}）"
        driver.emit(KIND_DRAFT, {"action": "confirm", "id": int(draft_id)})
        driver._note_tool_result("confirm_draft", f"#{draft_id} 准奏入档")
        return f"已准奏 #{draft_id} 入档。"

    def reject_draft(draft_id: int) -> str:
        """驳回：驳回大臣拟旨的 pending 草案，可在对话里让其重拟。"""
        driver._note_tool_call("reject_draft", {"id": draft_id})
        try:
            session.reject_directive(int(draft_id))
        except Exception as e:  # noqa: BLE001
            return f"（驳回失败：{e}）"
        driver.emit(KIND_DRAFT, {"action": "reject", "id": int(draft_id)})
        driver._note_tool_result("reject_draft", f"#{draft_id} 已驳回")
        return f"已驳回 #{draft_id}。可在对话里向大臣说明理由，让其重拟。"

    def add_draft(text: str) -> str:
        """自行起草一道诏令（仅在大臣拟旨不合意时用）。须完整：从"奉天承运皇帝诏曰："到"钦此。"，含执行者、动作、银粮数额、期限。"""
        body = str(text or "").strip()
        driver._note_tool_call("add_draft", {"text": _trim(body, 80)})
        if not body:
            return "（草案正文不能为空。）"
        try:
            view = session.add_directive(body, notes="AI 皇帝亲拟")
        except Exception as e:  # noqa: BLE001
            return f"（新增草案失败：{e}）"
        driver.emit(KIND_DRAFT, {"action": "add", "id": view.id, "text": _trim(view.text, 200)})
        driver._note_tool_result("add_draft", f"#{view.id}：{_trim(view.text, 100)}")
        return f"已亲拟草案 #{view.id}：{_trim(view.text, 120)}"

    def delete_draft(draft_id: int) -> str:
        """删除本月一条草案（id 见 list_drafts 输出中 # 后的编号）。"""
        driver._note_tool_call("delete_draft", {"id": draft_id})
        try:
            session.delete_directive(int(draft_id))
        except Exception as e:  # noqa: BLE001
            return f"（删除失败：{e}）"
        driver.emit(KIND_DRAFT, {"action": "delete", "id": int(draft_id)})
        driver._note_tool_result("delete_draft", f"#{draft_id} 已删除")
        return f"已删除草案 #{draft_id}。"

    # ── 推进时间 ────────────────────────────────────────────────────────
    def issue_decree() -> str:
        """颁诏并进入月度结算（财政/军队/地方/朝局/外部势力）。无草案则按"本月无新诏"推进；尚有 pending 拟旨未核定时会被拒绝。"""
        driver._note_tool_call("issue_decree", {})
        try:
            result = session.resolve_turn()
        except ValueError as e:
            return f"（不能颁诏：{e}）"
        except LLMUnavailable as e:
            return f"（月末推演失败：{e}）"
        except Exception as e:  # noqa: BLE001
            return f"（月末推演异常：{e}）"
        if result.awaiting:
            decisions = list(result.decisions or [])
            driver.emit(KIND_DECISION, {"decisions": decisions})
            simplified = [
                {
                    "idx": int(d.get("idx", i)),
                    "title": str(d.get("title", "")),
                    "context": _trim(d.get("context", ""), 300),
                    "options": [
                        {"label": str(o.get("label", "")), "hint": str(o.get("hint", ""))}
                        for o in (d.get("options") or [])
                    ],
                }
                for i, d in enumerate(decisions)
            ]
            return (
                "【推演出现决策点，请陛下亲裁】\n"
                + json.dumps({"decisions": simplified}, ensure_ascii=False, indent=1)
                + "\n请用 submit_decisions(choices_json) 逐项裁定，choices_json 形如 "
                '[{"idx": 0, "label": "选项名"}]，label 须与选项原文一致。'
            )
        report = str(result.report or "")
        decree = str(getattr(session, "last_decree", "") or "")
        try:
            game.refresh_turn()
        except Exception:
            pass
        driver._mark_turn_issued(report=report, decree=decree)
        driver._note_tool_result("issue_decree", "颁诏完成，进入下一月")
        return f"【颁诏完成·月末战报】\n{_trim(report, 2500)}\n{_metrics_line(game)}"

    def submit_decisions(choices_json: str) -> str:
        """对 issue_decree 出现的决策点逐项亲裁。choices_json 为 JSON 数组字符串，每项 {"idx": 决策点序号, "label": 选中的选项名}，label 须与选项原文一致。"""
        driver._note_tool_call("submit_decisions", {"choices": _trim(choices_json, 120)})
        try:
            parsed = json.loads(str(choices_json or ""))
        except Exception:
            return '（choices_json 须为 JSON 数组字符串，如 [{"idx": 0, "label": "选项名"}]。）'
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list) or not parsed:
            return "（choices_json 为空或格式不对。）"
        try:
            parsed = sorted(
                (c for c in parsed if isinstance(c, dict)),
                key=lambda c: int(c.get("idx", 0) or 0),
            )
        except Exception:
            pass
        try:
            report = session.submit_decisions(parsed)
        except ValueError as e:
            return f"（亲裁失败：{e}）"
        except Exception as e:  # noqa: BLE001
            return f"（亲裁异常：{e}）"
        report = str(report or "")
        try:
            game.refresh_turn()
        except Exception:
            pass
        driver._mark_turn_issued(report=report, decree=str(getattr(session, "last_decree", "") or ""))
        driver._note_tool_result("submit_decisions", "亲裁完成，进入下一月")
        return f"【亲裁完成·月末战报】\n{_trim(report, 2500)}\n{_metrics_line(game)}"

    return [
        get_situation,
        list_ministers,
        chat_with_minister,
        list_drafts,
        confirm_draft,
        reject_draft,
        add_draft,
        delete_draft,
        issue_decree,
        submit_decisions,
    ]


def snap_metrics(game: Any) -> Dict[str, Any]:
    """驱动器/事件用的指标快照（国库/内库/民心/皇威 + 年月回合）。"""
    state = game.state
    return {
        "year": int(state.year),
        "period": int(state.period),
        "turn": int(state.turn),
        "phase": str(state.turn_phase),
        "ended": bool(getattr(state, "ended", False)),
        "metrics": {k: float(v) for k, v in dict(state.metrics).items()},
    }
