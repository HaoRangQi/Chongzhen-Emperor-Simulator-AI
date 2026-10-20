import React from "react";
import { forwardSteamEvents } from "./steamEvents";
import type {
  ApiErrorDetail,
  AutoplayEvent,
  AutoplayPersona,
  AutoplayStatusPayload,
  ChatResponse,
  CourtChatMessage,
  CourtChatResponse,
  LLMModelInfo,
  ScenarioChatResult,
  ScenarioFull,
  ScenarioGenerateResult,
  ScenarioManifest,
} from "./types";

declare global {
  interface Window {
    MING_API_BASE?: string;
  }
}

export class ApiRequestError extends Error {
  detail: ApiErrorDetail;

  constructor(detail: ApiErrorDetail, fallback: string) {
    const message = detail.message || fallback;
    super(detail.code ? `[${detail.code}] ${message}` : message);
    this.name = "ApiRequestError";
    this.detail = detail;
  }
}

export const normalizeApiError = (error: any, fallback: string): ApiErrorDetail => {
  const detail = error?.detail ?? error;
  if (detail && typeof detail === "object") {
    return {
      code: detail.code,
      message: detail.message || detail.detail || fallback,
      provider_message: detail.provider_message,
      status_code: detail.status_code,
    };
  }
  return { message: String(detail || fallback) };
};

export const formatApiError = (error: any, fallback: string) => {
  const detail = error instanceof ApiRequestError ? error.detail : normalizeApiError(error, fallback);
  return detail.code ? `[${detail.code}] ${detail.message || fallback}` : detail.message || fallback;
};

const normalizeApiBase = (value: string) => value.trim().replace(/\/+$/, "");

export const apiBase = () => {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get("api") || params.get("api_base") || "";
  const fromWindow = window.MING_API_BASE || "";
  const fromEnv = import.meta.env.VITE_API_BASE || "";
  return normalizeApiBase(fromQuery || fromWindow || fromEnv);
};

export const apiUrl = (path: string) => {
  if (/^https?:\/\//i.test(path)) return path;
  const base = apiBase();
  if (!base) return path;
  return `${base}${path.startsWith("/") ? path : `/${path}`}`;
};

export const api = async <T,>(path: string, options?: RequestInit): Promise<T> => {
  const response = await fetch(apiUrl(path), {
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiRequestError(normalizeApiError(error, response.statusText), response.statusText);
  }
  const payload = await response.json();
  void forwardSteamEvents(payload);
  return payload;
};

export const parseSseMessage = (raw: string): { event: string; data: string } | null => {
  const lines = raw.split(/\r?\n/);
  let event = "message";
  const dataLines: string[] = [];
  for (const line of lines) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) return null;
  return { event, data: dataLines.join("\n") };
};

export const streamChat = async (
  ministerName: string,
  message: string,
  onDelta: (delta: string) => void,
): Promise<ChatResponse> => {
  const response = await fetch(apiUrl(`/api/ministers/${encodeURIComponent(ministerName)}/chat/stream`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiRequestError(normalizeApiError(error, response.statusText), response.statusText);
  }
  if (!response.body) {
    throw new Error("浏览器不支持流式回复。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const messages = buffer.split("\n\n");
    buffer = messages.pop() || "";

    for (const messageBlock of messages) {
      const parsed = parseSseMessage(messageBlock);
      if (!parsed) continue;
      const payload = JSON.parse(parsed.data);
      if (parsed.event === "delta") {
        onDelta(String(payload.content || ""));
      } else if (parsed.event === "done") {
        return payload as ChatResponse;
      } else if (parsed.event === "error") {
        throw new ApiRequestError(normalizeApiError(payload, "流式回复失败。"), "流式回复失败。");
      }
    }

    if (done) break;
  }

  throw new Error("流式回复中断，未收到完成事件。");
};

export const streamCourtChat = async (
  message: string,
  ministers: string[],
  onReply: (reply: CourtChatMessage) => void,
  onDelta?: (speaker: string, delta: string) => void,
  onSpeaker?: (speaker: string) => void,
  onConclusion?: (message: CourtChatMessage) => void,
  signal?: AbortSignal,
): Promise<CourtChatResponse> => {
  const response = await fetch(apiUrl("/api/court_chat/stream"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, ministers }),
    signal,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiRequestError(normalizeApiError(error, response.statusText), response.statusText);
  }
  if (!response.body) {
    throw new Error("浏览器不支持流式朝会。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const messages = buffer.split("\n\n");
    buffer = messages.pop() || "";

    for (const messageBlock of messages) {
      const parsed = parseSseMessage(messageBlock);
      if (!parsed) continue;
      const payload = JSON.parse(parsed.data);
      if (parsed.event === "reply") {
        onReply(payload as CourtChatMessage);
      } else if (parsed.event === "conclusion") {
        onConclusion?.(payload as CourtChatMessage);
      } else if (parsed.event === "speaker") {
        onSpeaker?.(String(payload.speaker || ""));
      } else if (parsed.event === "delta") {
        onDelta?.(String(payload.speaker || ""), String(payload.content || ""));
      } else if (parsed.event === "done") {
        return payload as CourtChatResponse;
      } else if (parsed.event === "error") {
        throw new ApiRequestError(normalizeApiError(payload, "朝会回复失败。"), "朝会回复失败。");
      }
    }

    if (done) break;
  }

  throw new Error("朝会流式回复中断，未收到完成事件。");
};

export const summarizeCourtChat = async (messages: CourtChatMessage[]): Promise<CourtChatMessage> => {
  return api<CourtChatMessage>("/api/court_chat/summary", {
    method: "POST",
    body: JSON.stringify({
      messages: messages.map((message) => ({
        role: message.role,
        speaker: message.speaker,
        content: message.displayContent ?? message.content,
      })),
    }),
  });
};

// ---- AI 托管（观战模式） ----

export const getAutoplayStatus = () =>
  api<AutoplayStatusPayload>("/api/autoplay/status");
export const getAutoplayPersonas = () =>
  api<{ personas: AutoplayPersona[] }>("/api/autoplay/personas");

export const startAutoplay = (body: {
  goal?: string;
  persona?: string;
  max_turns?: number;
  max_steps_per_turn?: number;
  max_chats_per_turn?: number;
  token_budget?: number;
  step_delay?: number;
  transcript?: boolean;
}) =>
  api<{ ok: boolean; status: AutoplayStatusPayload }>("/api/autoplay/start", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const pauseAutoplay = () =>
  api<{ ok: boolean; status: AutoplayStatusPayload }>("/api/autoplay/pause", { method: "POST" });

export const resumeAutoplay = () =>
  api<{ ok: boolean; status: AutoplayStatusPayload }>("/api/autoplay/resume", { method: "POST" });

export const stopAutoplay = () =>
  api<{ ok: boolean; status: AutoplayStatusPayload }>("/api/autoplay/stop", { method: "POST" });

export const takeoverAutoplay = () =>
  api<{ ok: boolean; status: AutoplayStatusPayload }>("/api/autoplay/takeover", { method: "POST" });

/** 订阅观战事件流（SSE）。返回取消订阅函数。 */export const subscribeAutoplayEvents = (
  onEvent: (event: AutoplayEvent) => void,
  onError?: (message: string) => void,
): (() => void) => {
  const controller = new AbortController();
  const reader = (async () => {
    try {
      const response = await fetch(apiUrl("/api/autoplay/events"), { signal: controller.signal });
      if (!response.ok || !response.body) {
        const error = await response.json().catch(() => ({ detail: response.statusText }));
        throw new ApiRequestError(normalizeApiError(error, response.statusText), response.statusText);
      }
      const streamReader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await streamReader.read();
        if (done) break;
        buffer += decoder.decode(value || new Uint8Array(), { stream: true });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() || "";
        for (const block of blocks) {
          const parsed = parseSseMessage(block);
          if (!parsed) continue;
          try {
            onEvent(JSON.parse(parsed.data) as AutoplayEvent);
          } catch {
            // ping/注释帧等非 JSON 帧直接忽略
          }
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        onError?.(error instanceof Error ? error.message : String(error));
      }
    }
  })();
  void reader;
  return () => controller.abort();
};

// ---- LLM 网关：连接 JSON 直填 + 模型探测 ----

/** 干解析 Cherry Studio / new-api 连接 JSON：只拆 base_url/api_key，不保存。 */
export const parseConnJson = (connJson: string) =>
  api<{ base_url: string; api_key: string; has_key: boolean }>("/api/llm/parse_conn", {
    method: "POST",
    body: JSON.stringify({ conn_json: connJson }),
  });

/** 向网关 GET {base_url}/models 拉取全部可选模型。api_key 空=用已保存的。 */
export const fetchLlmModels = (body: { base_url: string; api_key?: string }) =>
  api<{ models: LLMModelInfo[]; count: number; endpoint: string }>("/api/llm/models", {
    method: "POST",
    body: JSON.stringify(body),
  });

// ---- 自定义剧本 ----

export const listScenarios = () =>
  api<{ scenarios: ScenarioManifest[]; active_id: string }>("/api/scenarios");

export const getScenario = (id: string) =>
  api<ScenarioFull>(`/api/scenarios/${encodeURIComponent(id)}`);

// copyFrom: ""=空白；"__default__"=复制默认（崇祯元年）；其余=复制该剧本 id。
export const createScenario = (name: string, description: string, copyFrom = "") =>
  api<{ manifest: ScenarioManifest; active_id: string }>("/api/scenarios", {
    method: "POST",
    body: JSON.stringify({ name, description, copy_from: copyFrom }),
  });

export const updateScenarioFile = (id: string, file: "characters" | "events" | "seed_events", content: unknown) =>
  api<{ manifest: ScenarioManifest }>(`/api/scenarios/${encodeURIComponent(id)}/${file}`, {
    method: "PUT",
    body: JSON.stringify({ content }),
  });

export const updateScenarioManifest = (id: string, patch: { name?: string; description?: string }) =>
  api<{ manifest: ScenarioManifest }>(`/api/scenarios/${encodeURIComponent(id)}/manifest`, {
    method: "PUT",
    body: JSON.stringify(patch),
  });

export const deleteScenario = (id: string) =>
  api<{ scenarios: ScenarioManifest[]; active_id: string }>(`/api/scenarios/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });

export const activateScenario = (id: string) =>
  api<{ active_id: string; scenarios: ScenarioManifest[] }>(`/api/scenarios/${encodeURIComponent(id)}/activate`, {
    method: "POST",
  });

export const deactivateScenario = () =>
  api<{ active_id: string; scenarios: ScenarioManifest[] }>("/api/scenarios/deactivate", {
    method: "POST",
  });

export const generateScenario = (req: { prompt: string; name?: string; description?: string; files?: string[] }) =>
  api<ScenarioGenerateResult>("/api/scenarios/generate", {
    method: "POST",
    body: JSON.stringify(req),
  });

export const streamScenarioChat = async (
  scenarioId: string,
  message: string,
  onDelta: (delta: string) => void,
): Promise<ScenarioChatResult> => {
  const response = await fetch(apiUrl(`/api/scenarios/${encodeURIComponent(scenarioId)}/chat/stream`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiRequestError(normalizeApiError(error, response.statusText), response.statusText);
  }
  if (!response.body) {
    throw new Error("浏览器不支持流式回复。");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const messages = buffer.split("\n\n");
    buffer = messages.pop() || "";
    for (const messageBlock of messages) {
      const parsed = parseSseMessage(messageBlock);
      if (!parsed) continue;
      const payload = JSON.parse(parsed.data);
      if (parsed.event === "delta") {
        onDelta(String(payload.content || ""));
      } else if (parsed.event === "done") {
        return payload as ScenarioChatResult;
      } else if (parsed.event === "error") {
        throw new ApiRequestError(normalizeApiError(payload, "对话失败。"), "对话失败。");
      }
    }
    if (done) break;
  }
  throw new Error("对话中断，未收到完成事件。");
};
