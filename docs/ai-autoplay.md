# AI 托管（观战模式）

让 AI 自己当崇祯：看局、召见、拟诏、颁诏全由 AI 决策，人类只围观——
每一步思考、每句奏对、每道诏书、每月战报都实时上屏，随时可暂停、停止或**接管亲政**。

## 为什么不是「脚本自动点」

项目里本来就有个 AI 自动玩的原型（`scripts/play_as_emperor.py`）：agno 皇帝 Agent 用
pexpect 驱动 CLI，靠解析终端文本决定下一条键盘输入。它能跑，但三个硬伤：

1. 文本解析脆弱——CLI 提示词一改，Agent 就误判该输什么（edit 覆盖残稿之类）；
2. 拿不到结构化局势——Agent 只能"看"屏幕，没有国库/欠饷/民变的机器可读视图；
3. 无法流式给人看——人类围观时只能等一整回合跑完。

本方案把「皇帝」做成一等公民：**进程内驱动器 + 结构化工具 + SSE 事件总线**，三条全部解决。

## 架构

```
┌─────────────┐   HTTP 控制    ┌──────────────┐   启动/暂停/停止   ┌───────────────────┐
│  观战面板    │ ◄──────────► │  web_app.py  │ ◄──────────────► │  AutoplayDriver   │
│ (React/SSE) │   事件流 SSE   │ (FastAPI)    │                  │ (后台线程+皇帝Agent)│
└─────────────┘               └──────────────┘                  └─────────┬─────────┘
                                                                          │ 直接调用
                                                                          ▼
                                                                 ┌───────────────────┐
                                                                 │ GameSession/WebGame│
                                                                 │ 国库·大臣·诏令·结算 │
                                                                 └───────────────────┘
```

- **`ming_sim/autoplay/driver.py`**：后台线程 + 回合循环 + 护栏 + 事件总线。
  皇帝 Agent 用**结构化工具**直接调 `GameSession`（与人类玩家同一条操作通道），
  不解析任何文本。
- **`ming_sim/autoplay/tools.py`**：10 个工具——`get_situation / list_ministers /
  chat_with_minister / list_drafts / confirm_draft / reject_draft / add_draft /
  delete_draft / issue_decree / submit_decisions`。大臣流式回复经 `chat_stream`
  原样转发为 `chat_delta` 事件，观战端延迟与人类玩时一致。
- **`ming_sim/autoplay/prompt.py`**：圣训 + 7 套人格预设（持重/多疑/强硬/仁慈/
  权谋/果决/忧深切），移植自 `play_as_emperor.py`。
- **`web_app.py`**：`/api/autoplay/*` 七个端点 + 写锁中间件。
- **`web/src/components/autoplayPanel.tsx`**：观战面板（事件时间线/指标/控制）+ 菜单
  「AI 托管」标签页 + 悬浮状态丸。

## 单一写入者（关键安全约定）

托管期间（running/paused），除 `/api/autoplay/*` 外的**所有写请求一律 409**——
同一时刻只有 driver 线程能改 `GameSession`。读接口放行，观战面板正常渲染。

暂停/停止/接管都在**安全点**（两次 `agent.run` 之间、工具入口）生效；每个工具调用
（一次对话/一条草案/一次颁诏）是原子的，所以接管只是"AI 在一步之内退朝"，
存档不会写坏，人从同一进度接着玩。

## 用法

### 网页

菜单（右上角）→「AI 托管」标签页 → 填圣训/选性格/设回合上限 →「开始 AI 托管」。
右下角出现悬浮状态丸，点它打开观战面板。面板内可暂停/继续/停止/接管。

### 无头 CLI

```bash
# 真实 LLM（先 export OPENAI_API_KEY 等）
python3 scripts/autoplay_run.py --goal "一年内剪除魏忠贤+辽东欠饷减半" --turns 10 --persona schemer

# 无 key 冒烟（验证事件总线/工具链/护栏，不调 LLM）
python3 scripts/autoplay_run.py --mock --turns 2
```

Ctrl+C 随时停（等价于网页「停止/接管」）。

## 事件协议（SSE）

`GET /api/autoplay/events`，新连接先重放最近 500 条。每条事件
`{kind, ts, turn, data}`：

| kind | 含义 | data 要点 |
| --- | --- | --- |
| `turn_start` | 新月开局 | year/period/turn/goal/metrics/situation |
| `thought_delta` / `thought_done` | 圣心独白（流式/整段） | content |
| `tool_call` / `tool_result` | 工具调用/结果 | tool/args、summary |
| `chat_start` / `chat_delta` / `chat_done` | 召见对话 | minister/message、content、answer |
| `draft` | 草案增删准驳 | action/id/text |
| `issued` | 颁诏 | decree |
| `decision` | 推演出决策点 | decisions[idx/title/options] |
| `report` | 月末战报 | report/month_start_metrics/metrics_after |
| `status` / `error` / `finished` | 状态/出错/结束 | status/reason/message |

## 护栏

| 机制 | 默认 | 说明 |
| --- | --- | --- |
| 每月步数预算 | 4 次 agent.run | 防反复召见绕圈；耗尽自动暂停等人工。最后一步会强令「不要再召见，直接颁诏」 |
| 每月召见上限 | 6 次 | 开局表单可调；超限后召见工具直接回「请颁诏」，防止模型沉迷闲聊不收尾 |
| 连续错误熔断 | 3 次 | LLM 连续失败自动暂停（不退存档） |
| 空转熔断 | 连续 2 步零工具 | AI 一步都没动手（模型不可用/不支持工具调用）就停，不空耗 |
| 开跑前置校验 | 开 | 启动时短超时探测模型，模型名错误立即报错，不白跑四步 |
| 回合上限 | 0=不限 | `max_turns` 到点自动结束 |
| token 预算 | 0=不限 | `token_budget` 到点自动暂停 |
| 结局自停 | — | `state.ended` 时以结局原因收尾 |
| 观战语言纪律 | 开 | 模型思考链（reasoner 类模型常用英文思考）与工具调用前导文本一律不上屏；圣心独白只展示含汉字的内容，提示词里也明确要求全程中文。仅过滤展示，不影响游戏状态 |

## 配置

| 环境变量 | 说明 |
| --- | --- |
| `MING_SIM_EMPEROR_MODEL` / `_BASE_URL` / `_API_KEY` | 皇帝 Agent 专用模型（默认跟游戏主模型一致，低成本长线；想换更强模型时配） |

Token 记账走 `ming_sim/token_stats.py`（agno metrics 口径，含缓存命中），
观战面板实时显示；`scripts/autoplay_run.py --transcript` 可把事件流落盘 jsonl 复盘。

## 测试

- `python3 scripts/autoplay_run.py --mock --turns 2`：无 LLM 冒烟，
  验证驱动器循环/事件总线/工具链/草案/护栏。
- `python3 -m pytest tests/`：全量回归（本功能不改游戏内核，全绿）。

## 已知边界 / 后续

- steam 成就：托管回合暂不计入 steam stats（观战事件走独立 SSE 通道）。
- `step_delay` 在开始时设定，运行中不可调（面板内可暂停/续）。
- 可扩展：多人格对局擂台（两个 AI 皇帝先后跑同一存档比分）、
  目标达成度自动评分、事件流录屏导出。
