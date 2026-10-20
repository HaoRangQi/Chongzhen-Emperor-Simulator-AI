import React from "react";
import { Brain, Eye, Search, Wrench } from "lucide-react";
import type { LLMModelInfo } from "../types";

// 模型能力启发式（new-api / Cherry Studio 常见命名）：用于在选择器里打标。
export const modelCaps = (id: string) => {
  const low = id.toLowerCase();
  const nonChat = /(embed|rerank|tts|whisper|moderation|dall-e|dalle|sora|midjourney|flux|stable-diffusion|cogvideo|hunyuan-image)/.test(low);
  const vision = /(vl\b|vision|visual|gpt-4o|gpt-4\.1|gemini|claude-3|qwen-vl|glm-4v|internvl|pixtral|llava|kimi-vl)/.test(low);
  const reasoning = /(reasoner|reasoning|\br1\b|thinking|\bo1\b|\bo3\b|\bo4\b|qwq|glm-z1|glm-4\.5|deepseek-r|grok-3-mini)/.test(low);
  return { nonChat, vision, reasoning, tools: !nonChat };
};

// 厂商分组：先按 : / 切，再按空格切；无分隔时按第一个 - 切（glm-5.3 → glm）。
export const vendorOf = (id: string): string => {
  const head0 = id.split(/[:\/]/)[0].trim();
  if (!head0) return "其他";
  const head = /\s/.test(head0) ? head0.split(/\s+/)[0] : (head0.includes("-") ? head0.split("-")[0] : head0);
  return head || "其他";
};

// 网关模型选择器：搜索 + 按厂商分组 + 能力打标（视觉/推理/工具），点选即填入 Model。
// 主菜单（开局前）与游戏内菜单（LLM 配置）共用。
export function ModelPicker({
  models, current, search, onSearch, onPick, onClose,
}: {
  models: LLMModelInfo[];
  current: string;
  search: string;
  onSearch: (value: string) => void;
  onPick: (id: string) => void;
  onClose: () => void;
}) {
  const chatModels = React.useMemo(() => models.filter((m) => !modelCaps(m.id).nonChat), [models]);
  const hidden = models.length - chatModels.length;
  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return chatModels;
    return chatModels.filter((m) => m.id.toLowerCase().includes(q));
  }, [chatModels, search]);
  const groups = React.useMemo(() => {
    const map = new Map<string, LLMModelInfo[]>();
    for (const m of filtered) {
      const vendor = vendorOf(m.id);
      const rows = map.get(vendor) || [];
      rows.push(m);
      map.set(vendor, rows);
    }
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0], "zh-Hans-CN"));
  }, [filtered]);

  return (
    <div className="llm-models">
      <div className="llm-models-head">
        <Search size={13} className="llm-models-search-icon" />
        <input
          className="menu-input"
          placeholder="搜索模型…"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          autoFocus
        />
        <span className="menu-hint">
          {filtered.length}/{chatModels.length}
          {hidden > 0 ? `（已过滤 ${hidden} 个非对话模型）` : ""}
        </span>
        <button className="menu-btn" type="button" onClick={onClose}>收起</button>
      </div>
      <div className="llm-models-body">
        {groups.map(([vendor, rows]) => (
          <div className="llm-models-group" key={vendor}>
            <div className="llm-models-vendor">
              <b>{vendor}</b>
              <span className="menu-hint">{rows.length}</span>
            </div>
            {rows.map((m) => {
              const caps = modelCaps(m.id);
              return (
                <button
                  key={m.id}
                  type="button"
                  className={`llm-model-row${m.id === current ? " active" : ""}`}
                  onClick={() => onPick(m.id)}
                  title={m.id}
                >
                  <span className="llm-model-id">{m.id}</span>
                  <span className="llm-model-caps">
                    {caps.vision ? <Eye size={12} aria-label="视觉" /> : null}
                    {caps.reasoning ? <Brain size={12} aria-label="推理" /> : null}
                    {caps.tools ? <Wrench size={12} aria-label="工具调用" /> : null}
                  </span>
                </button>
              );
            })}
          </div>
        ))}
        {filtered.length === 0 ? <div className="menu-hint llm-models-empty">没有匹配的模型</div> : null}
      </div>
    </div>
  );
}
