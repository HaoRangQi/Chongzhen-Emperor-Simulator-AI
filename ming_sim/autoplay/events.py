"""观战事件协议：AutoplayDriver -> 订阅者（SSE）-> 前端观战面板。L8。

每个事件统一为 {"kind", "ts", "turn", "data"}：
- kind  事件类型（下方常量）
- ts    Unix 时间戳（秒，保留毫秒）
- turn  事件发生时的游戏回合号
- data  事件负载（按 kind 而定）

事件目录：
- turn_start      新月开始：年月/指标/上月奏报摘要/目标
- thought_delta   皇帝 Agent 内心独白增量（流式）
- thought_done    内心独白整段结束
- tool_call       工具调用开始：工具名 + 参数摘要
- tool_result     工具调用结束：结果摘要
- chat_start      开始召见某大臣
- chat_delta      大臣奏对增量（流式，前端复用聊天气泡）
- chat_done       本次召对结束（含大臣拟旨等副作用摘要）
- draft           草案变化：新增 / 删除
- issued          颁诏：诏书正文
- decision        推演出现决策点（HITL），附选项
- report          月末战报 + 结算后指标
- status          驱动器状态变化（running/paused/stopped/finished/error）
- error           出错（LLM 失败、护栏触发等）
- finished         托管结束（结局/回合上限/主动停止）
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

KIND_TURN_START = "turn_start"
KIND_THOUGHT_DELTA = "thought_delta"
KIND_THOUGHT_DONE = "thought_done"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_CHAT_START = "chat_start"
KIND_CHAT_DELTA = "chat_delta"
KIND_CHAT_DONE = "chat_done"
KIND_DRAFT = "draft"
KIND_ISSUED = "issued"
KIND_DECISION = "decision"
KIND_REPORT = "report"
KIND_STATUS = "status"
KIND_ERROR = "error"
KIND_FINISHED = "finished"

ALL_KINDS = (
    KIND_TURN_START,
    KIND_THOUGHT_DELTA,
    KIND_THOUGHT_DONE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KIND_CHAT_START,
    KIND_CHAT_DELTA,
    KIND_CHAT_DONE,
    KIND_DRAFT,
    KIND_ISSUED,
    KIND_DECISION,
    KIND_REPORT,
    KIND_STATUS,
    KIND_ERROR,
    KIND_FINISHED,
)


def make_event(kind: str, turn: int, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "kind": kind,
        "ts": round(time.time(), 3),
        "turn": int(turn),
        "data": data or {},
    }
