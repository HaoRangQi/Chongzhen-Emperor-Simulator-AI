import React from "react";
import { Bot, Crown, Pause, Play, Square, User, X } from "lucide-react";
import {
  getAutoplayPersonas,
  getAutoplayStatus,
  pauseAutoplay,
  resumeAutoplay,
  startAutoplay,
  stopAutoplay,
  subscribeAutoplayEvents,
  takeoverAutoplay,
} from "../api";
import type { AutoplayEvent, AutoplayEventKind, AutoplayPersona, AutoplayStatusPayload } from "../types";

// ── 观战流条目：把 SSE 事件流折叠成时间线卡片 ──────────────────────
type FeedItem = {
  id: string;
  kind: AutoplayEventKind;
  ts: number;
  turn: number;
  text: string;
  meta?: string;
};

const PERSONA_DEFAULT_KEY = "balanced";

const fmtMoney = (value: number): string => {
  const n = Number(value) || 0;
  return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)} 万两`;
};

const fmtClock = (seconds: number): string => {
  const s = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h > 0 ? `${h} 时 ${m} 分` : m > 0 ? `${m} 分 ${r} 秒` : `${r} 秒`;
};

// 指标涨跌着色：涨红跌绿（A 股惯例）。
const deltaClass = (delta: number): string => (delta >= 0 ? "ap-delta-up" : "ap-delta-down");
const deltaText = (delta: number, unit = ""): string =>
  `${delta >= 0 ? "+" : ""}${delta >= 100 || delta <= -100 ? delta.toFixed(0) : delta.toFixed(1)}${unit}`;

/** 把一条事件折进时间线：流式 delta 类事件合并进上一条同源卡片。 */
function foldEvent(items: FeedItem[], event: AutoplayEvent): FeedItem[] {
  const d = event.data || {};
  const seq = items.length;
  let text = "";
  let meta: string | undefined;
  switch (event.kind) {
    case "turn_start":
      text = `${d.year} 年 ${d.period} 月`;
      meta = d.goal || "";
      break;
    case "thought_delta": {
      const last = items[items.length - 1];
      if (last && last.kind === "thought_delta") {
        const grown = [...items];
        grown[grown.length - 1] = { ...last, text: last.text + String(d.content || "") };
        return grown;
      }
      text = String(d.content || "");
      break;
    }
    case "thought_done":
      text = String(d.content || "");
      break;
    case "tool_call":
      text = String(d.tool || "");
      meta = JSON.stringify(d.args || {}).slice(0, 120);
      break;
    case "tool_result":
      text = String(d.summary || "");
      break;
    case "chat_start":
      text = `${d.minister || ""}`;
      meta = String(d.message || "");
      break;
    case "chat_delta": {
      const last = items[items.length - 1];
      if (last && last.kind === "chat_delta") {
        const grown = [...items];
        grown[grown.length - 1] = { ...last, text: last.text + String(d.content || "") };
        return grown;
      }
      text = String(d.content || "");
      meta = String(d.minister || "");
      break;
    }
    case "chat_done":
      text = String(d.answer || "");
      meta = String(d.minister || "");
      break;
    case "draft":
      text = `草案 ${d.action} #${d.id}`;
      break;
    case "issued":
      text = String(d.decree || "");
      break;
    case "decision": {
      const decisions = Array.isArray(d.decisions) ? d.decisions : [];
      text = decisions
        .map((x: any) => `【${x.title || "待裁"}】${(x.options || []).map((o: any) => o.label).join(" / ")}`)
        .join("\n");
      break;
    }
    case "report":
      text = String(d.report || "");
      break;
    case "status":
      text = `${d.status || ""}${d.reason ? ` · ${d.reason}` : ""}`;
      break;
    case "error":
      text = String(d.message || "");
      break;
    case "finished":
      text = String(d.reason || "");
      break;
    default:
      text = "";
  }
  return [...items, { id: `${event.kind}-${event.ts}-${seq}`, kind: event.kind, ts: event.ts, turn: event.turn, text, meta }];
}

// ── 游戏菜单「AI 托管」标签页 ─────────────────────────────────────
export function AutoplayTab({ onOpenPanel }: { onOpenPanel: () => void }) {
  const [personas, setPersonas] = React.useState<AutoplayPersona[]>([]);
  const [goal, setGoal] = React.useState(() => localStorage.getItem("autoplayGoal") || "");
  const [persona, setPersona] = React.useState(() => localStorage.getItem("autoplayPersona") || PERSONA_DEFAULT_KEY);
  const [maxTurns, setMaxTurns] = React.useState("0");
  const [maxChats, setMaxChats] = React.useState(() => localStorage.getItem("autoplayMaxChats") || "6");
  const [stepDelay, setStepDelay] = React.useState("0.5");
  const [status, setStatus] = React.useState<AutoplayStatusPayload | null>(null);
  const [busy, setBusy] = React.useState("");
  const [error, setError] = React.useState("");

  const refresh = React.useCallback(async () => {
    try {
      setStatus(await getAutoplayStatus());
    } catch {
      // 后端未就绪时静默，菜单不弹错
    }
  }, []);

  React.useEffect(() => {
    getAutoplayPersonas().then((data) => setPersonas(data.personas || [])).catch(() => {});
    refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const act = async (name: string, fn: () => Promise<any>) => {
    setBusy(name);
    setError("");
    try {
      const data = await fn();
      setStatus(data.status);
      await refresh();
    } catch (e: any) {
      setError(e?.message || `${name}失败`);
    } finally {
      setBusy("");
    }
  };

  const onStart = async () => {
    setBusy("start");
    setError("");
    try {
      localStorage.setItem("autoplayGoal", goal);
      localStorage.setItem("autoplayPersona", persona);
      localStorage.setItem("autoplayMaxChats", maxChats);
      const data = await startAutoplay({
        goal,
        persona,
        max_turns: Math.max(0, Number(maxTurns) || 0),
        max_chats_per_turn: Math.max(1, Number(maxChats) || 6),
        step_delay: Math.max(0, Number(stepDelay) || 0),
      });
      setStatus(data.status);
      onOpenPanel();
    } catch (e: any) {
      setError(e?.message || "启动失败");
    } finally {
      setBusy("");
    }
  };

  const active = !!status?.active;
  const st = status?.status || "idle";

  return (
    <div className="ap-tab">
      <p className="ap-tab-intro">
        让 AI 自己当崇祯：看局、召见、拟诏、颁诏全由 AI 决策，你只管围观——
        每一步思考、每句奏对、每道诏书都实时呈现，随时可暂停或接管亲政。
      </p>
      {error ? <p className="ap-error">{error}</p> : null}

      {active || st === "paused" ? (
        <div className="ap-running">
          <div className="ap-running-head">
            <span className={`ap-badge ap-badge-${st}`}>
              {st === "running" ? "托管进行中" : st === "paused" ? "已暂停" : st}
            </span>
            <b>{status?.persona_label || status?.persona}</b>
            <span className="ap-dim">
              {status?.year} 年 {status?.period} 月 · 第 {status?.turn} 回合
            </span>
          </div>
          <p className="ap-goal-line">本局目标：{status?.goal || "（未设定）"}</p>
          {status?.reason ? <p className="ap-dim">{status.reason}</p> : null}
          <div className="ap-btn-row">
            {st === "running" ? (
              <button className="ap-btn" disabled={busy === "pause"} onClick={() => act("pause", pauseAutoplay)}>
                <Pause size={14} /> 暂停
              </button>
            ) : (
              <button className="ap-btn ap-btn-primary" disabled={busy === "resume"} onClick={() => act("resume", resumeAutoplay)}>
                <Play size={14} /> 继续托管
              </button>
            )}
            <button className="ap-btn" disabled={busy === "stop"} onClick={() => act("stop", stopAutoplay)}>
              <Square size={14} /> 停止
            </button>
            <button
              className="ap-btn ap-btn-danger"
              disabled={busy === "takeover"}
              onClick={() => {
                if (window.confirm("接管后 AI 立即退朝（当前动作跑完后停），之后由你亲自执政。确定接管？")) {
                  act("takeover", takeoverAutoplay);
                }
              }}
            >
              <User size={14} /> 接管亲政
            </button>
            <button className="ap-btn" onClick={onOpenPanel}>打开观战面板</button>
          </div>
        </div>
      ) : (
        <div className="ap-form">
          <label className="ap-field">
            <span>本局目标（写给 AI 的圣训）</span>
            <textarea
              rows={3}
              value={goal}
              placeholder="例：一年内剪除魏忠贤、辽东欠饷减半、陕西民变不失控"
              onChange={(e) => setGoal(e.target.value)}
            />
          </label>
          <div className="ap-field-row">
            <label className="ap-field">
              <span>AI 性格</span>
              <select value={persona} onChange={(e) => setPersona(e.target.value)}>
                {(personas.length ? personas : [{ key: PERSONA_DEFAULT_KEY, label: "持重" }]).map((p) => (
                  <option key={p.key} value={p.key}>{p.label}</option>
                ))}
              </select>
            </label>
            <label className="ap-field">
              <span>最多托管回合（0=不限）</span>
              <input value={maxTurns} onChange={(e) => setMaxTurns(e.target.value)} inputMode="numeric" />
            </label>
            <label className="ap-field">
              <span>每月召见上限（防闲聊拖延）</span>
              <input value={maxChats} onChange={(e) => setMaxChats(e.target.value)} inputMode="numeric" />
            </label>
            <label className="ap-field">
              <span>步间延时（秒，放慢方便看）</span>
              <input value={stepDelay} onChange={(e) => setStepDelay(e.target.value)} inputMode="decimal" />
            </label>
          </div>
          <button className="ap-btn ap-btn-primary ap-start" disabled={busy === "start"} onClick={onStart}>
            <Crown size={14} /> {busy === "start" ? "AI 升座中……" : "开始 AI 托管"}
          </button>
          <p className="ap-dim">
            托管期间人类操作会被锁定（409），点「接管亲政」随时拿回控制权；存档安全，可随时续跑。
          </p>
        </div>
      )}
    </div>
  );
}

// ── 观战面板：实时事件流 + 指标 + 控制 ────────────────────────────
export function AutoplayPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [status, setStatus] = React.useState<AutoplayStatusPayload | null>(null);
  const [items, setItems] = React.useState<FeedItem[]>([]);
  const [busy, setBusy] = React.useState("");
  const [error, setError] = React.useState("");
  const feedRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 状态轮询（按钮态 / 指标 / 计数）
  React.useEffect(() => {
    if (!open) return;
    let alive = true;
    const poll = async () => {
      try {
        const s = await getAutoplayStatus();
        if (alive) setStatus(s);
      } catch {
        // 后端未就绪
      }
    };
    poll();
    const timer = window.setInterval(poll, 2000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [open]);

  // 事件流订阅（连接即重放历史，晚打开不掉队）
  React.useEffect(() => {
    if (!open) return;
    setItems([]);
    const unsubscribe = subscribeAutoplayEvents(
      (event) => {
        if (event.kind === "hello") return;
        setItems((prev) => foldEvent(prev, event).slice(-600));
        if (event.kind === "finished" || event.kind === "status" || event.kind === "error") {
          getAutoplayStatus().then(setStatus).catch(() => {});
        }
      },
      (message) => setError(message),
    );
    return unsubscribe;
  }, [open]);

  // 自动滚到底
  React.useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items]);

  const act = async (name: string, fn: () => Promise<any>) => {
    setBusy(name);
    try {
      const data = await fn();
      setStatus(data.status);
    } catch (e: any) {
      setError(e?.message || `${name}失败`);
    } finally {
      setBusy("");
    }
  };

  if (!open) return null;
  const st = status?.status || "idle";
  const active = !!status?.active;
  const metrics = status?.metrics || {};
  const badgeText =
    st === "running" ? "托管进行中" : st === "paused" ? "已暂停" : st === "finished" ? "已结束" : st === "error" ? "出错了" : st === "stopped" ? "已停止" : st;

  return (
    <section className="ap-layer" role="dialog" aria-modal="true" aria-label="AI 托管观战面板">
      <div className="ap-scrim" onClick={onClose} />
      <div className="ap-window">
        <header className="ap-head">
          <div className="ap-title">
            <Crown size={18} />
            <h2>AI 托管观战</h2>
            <span className={`ap-badge ap-badge-${st}`}>{badgeText}</span>
            <span className="ap-dim">
              {status?.year || "—"} 年 {status?.period || "—"} 月 · 第 {status?.turn ?? "—"} 回合
              {status?.persona_label ? ` · ${status.persona_label}` : ""}
            </span>
          </div>
          <div className="ap-head-actions">
            {active && st === "running" ? (
              <button className="ap-btn" disabled={busy === "pause"} onClick={() => act("pause", pauseAutoplay)}>
                <Pause size={14} /> 暂停
              </button>
            ) : null}
            {active && st === "paused" ? (
              <button className="ap-btn ap-btn-primary" disabled={busy === "resume"} onClick={() => act("resume", resumeAutoplay)}>
                <Play size={14} /> 继续
              </button>
            ) : null}
            {active ? (
              <>
                <button className="ap-btn" disabled={busy === "stop"} onClick={() => act("stop", stopAutoplay)}>
                  <Square size={14} /> 停止
                </button>
                <button
                  className="ap-btn ap-btn-danger"
                  disabled={busy === "takeover"}
                  onClick={() => {
                    if (window.confirm("接管后 AI 立即退朝（当前动作跑完后停），之后由你亲自执政。确定接管？")) {
                      act("takeover", takeoverAutoplay);
                    }
                  }}
                >
                  <User size={14} /> 接管
                </button>
              </>
            ) : null}
            <button className="icon-button" aria-label="关闭观战面板" onClick={onClose}>
              <X size={16} />
            </button>
          </div>
        </header>

        <div className="ap-stats">
          <span className="ap-stat"><i>国库</i><b>{fmtMoney(metrics["国库"] || 0)}</b></span>
          <span className="ap-stat"><i>民心</i><b>{Math.round(metrics["民心"] || 0)}</b></span>
          <span className="ap-stat"><i>皇威</i><b>{Math.round(metrics["皇威"] || 0)}</b></span>
          <span className="ap-stat"><i>已托管</i><b>{fmtClock(status?.elapsed_sec || 0)}</b></span>
          <span className="ap-stat"><i>本月步数</i><b>{status?.steps_this_turn ?? 0}/{status?.max_steps_per_turn ?? "—"}</b></span>
          <span className="ap-stat"><i>本月召见</i><b>{status?.chats_this_turn ?? 0}/{status?.max_chats_per_turn ?? "—"}</b></span>
          <span className="ap-stat"><i>工具调用</i><b>{status?.tool_calls ?? 0}</b></span>
          <span className="ap-stat"><i>Tokens</i><b>{status?.tokens?.total ?? 0}</b></span>
        </div>
        {status?.goal ? <p className="ap-goal-line">圣训：{status.goal}</p> : null}
        {status?.reason ? <p className="ap-reason">{status.reason}</p> : null}
        {error ? <p className="ap-error">{error}</p> : null}
        {active ? <p className="ap-lock-note">托管进行中：人类游戏操作已锁定，可随时暂停或接管。</p> : null}
        {active ? (
          <p className="ap-hint-line">
            关闭此面板不影响托管——AI 在服务器后台继续运行；随时点右下角「AI 托管中」状态丸回来查看（重开会回放最近进度）。
          </p>
        ) : null}

        <div className="ap-feed" ref={feedRef}>
          {items.length === 0 ? <p className="ap-dim ap-feed-empty">等待事件……</p> : null}
          {items.map((item) => (
            <FeedCard key={item.id} item={item} />
          ))}
        </div>
      </div>
    </section>
  );
}

function FeedCard({ item }: { item: FeedItem }) {
  switch (item.kind) {
    case "turn_start":
      return (
        <div className="ap-card ap-month">
          <b>{item.text}</b>
          {item.meta ? <span className="ap-dim">目标：{item.meta}</span> : null}
        </div>
      );
    case "thought_delta":
    case "thought_done":
      return (
        <div className="ap-card ap-thought">
          <span className="ap-card-tag"><Bot size={12} /> 圣心独白</span>
          <p>{item.text}</p>
        </div>
      );
    case "tool_call":
      return (
        <div className="ap-card ap-tool">
          <span className="ap-card-tag">工具</span>
          <b>{item.text}</b>
          {item.meta ? <span className="ap-dim ap-tool-args">{item.meta}</span> : null}
        </div>
      );
    case "tool_result":
      return <div className="ap-card ap-tool-result">{item.text}</div>;
    case "chat_start":
    case "chat_delta":
    case "chat_done":
      return (
        <div className="ap-card ap-chat">
          <span className="ap-card-tag"><Crown size={12} /> 陛下问</span>
          <p className="ap-chat-ask">{item.kind === "chat_start" ? `${item.text}：${item.meta || ""}` : ""}</p>
          {item.kind === "chat_start" ? null : (
            <>
              <span className="ap-card-tag ap-chat-tag-minister"><Bot size={12} /> {item.meta || "大臣"} 奏对</span>
              <p className="ap-chat-answer">{item.text}</p>
            </>
          )}
        </div>
      );
    case "draft":
      return <div className="ap-card ap-draft">{item.text}</div>;
    case "issued":
      return (
        <div className="ap-card ap-decree">
          <span className="ap-card-tag">颁诏</span>
          <p>{item.text}</p>
        </div>
      );
    case "decision":
      return (
        <div className="ap-card ap-decision">
          <span className="ap-card-tag">决策点</span>
          <p>{item.text}</p>
        </div>
      );
    case "report":
      return <ReportCard item={item} />;
    case "status":
      return <div className="ap-card ap-status">{item.text}</div>;
    case "error":
      return <div className="ap-card ap-error">{item.text}</div>;
    case "finished":
      return (
        <div className="ap-card ap-finished">
          <b>托管结束</b>
          <p>{item.text}</p>
        </div>
      );
    default:
      return null;
  }
}

// 月末战报卡：指标涨跌按涨红跌绿着色。
function ReportCard({ item }: { item: FeedItem }) {
  const [open, setOpen] = React.useState(true);
  return (
    <div className="ap-card ap-report">
      <button className="ap-report-toggle" onClick={() => setOpen((v) => !v)}>
        <span className="ap-card-tag">月末战报</span>
        <span className="ap-dim">{open ? "收起" : "展开"}</span>
      </button>
      {open ? <p className="ap-report-body">{item.text}</p> : null}
    </div>
  );
}

// 悬浮态：托管进行中且面板关闭时，显示可点击的实时状态丸。
export function AutoplayFab({ onClick }: { onClick: () => void }) {
  const [status, setStatus] = React.useState<AutoplayStatusPayload | null>(null);
  React.useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const s = await getAutoplayStatus();
        if (alive) setStatus(s);
      } catch {
        // 后端未就绪
      }
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);
  if (!status || !status.active) return null;
  return (
    <button
      className={`ap-fab ap-fab-${status.status}`}
      onClick={onClick}
      title="AI 正在进行托管，点此打开观战面板"
    >
      <Crown size={14} />
      <span>AI 托管{status.status === "paused" ? "已暂停" : "中"}</span>
      <span className="ap-dim">
        {status.year}.{String(status.period).padStart(2, "0")} · 第 {status.turn} 回合
      </span>
    </button>
  );
}
