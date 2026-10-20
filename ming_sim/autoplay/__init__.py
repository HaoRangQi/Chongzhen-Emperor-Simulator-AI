"""AI 自动托管（观战模式）子包：driver 在后台线程驱动 GameSession，
每一步动作都转成事件，经 SSE 推给前端观战面板。L8。

- driver.py   AutoplayDriver：后台线程 + 回合循环 + 暂停/停止/接管 + 护栏
- tools.py    皇帝 Agent 的结构化工具（直接调 GameSession，不解析文本）
- prompt.py   皇帝系统提示词与人格预设（移植自 scripts/play_as_emperor.py）
- events.py   事件协议（kind 常量 + make_event）
"""

from .driver import AutoplayConfig, AutoplayDriver
from .events import (
    KIND_CHAT_DELTA,
    KIND_CHAT_DONE,
    KIND_CHAT_START,
    KIND_DECISION,
    KIND_DRAFT,
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

__all__ = [
    "AutoplayConfig",
    "AutoplayDriver",
    "KIND_CHAT_DELTA",
    "KIND_CHAT_DONE",
    "KIND_CHAT_START",
    "KIND_DECISION",
    "KIND_DRAFT",
    "KIND_ERROR",
    "KIND_FINISHED",
    "KIND_ISSUED",
    "KIND_REPORT",
    "KIND_STATUS",
    "KIND_THOUGHT_DELTA",
    "KIND_THOUGHT_DONE",
    "KIND_TOOL_CALL",
    "KIND_TOOL_RESULT",
    "KIND_TURN_START",
    "make_event",
]
