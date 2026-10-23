import { authorizedFetch } from "./authorizedFetch";
import type { ConversationAttachment } from "./attachmentsApi";
import { SessionApiError } from "./sessionApi";

export interface ContinuationSource { kind: "conversation" | "archive"; id: string; projectId: string }
export interface ContinuationPreview {
  token: string; title: string; context: string; includedMessages: number; totalMessages: number;
  capabilities: { type: string; name: string }[]; maxChars: number;
  excludedCapabilities?: { type: string; name: string }[];
  assets?: { id: string; name: string; size: number; available: boolean; compatible: boolean }[];
}
export interface ContinuationRecord { source: ContinuationSource; sourceTitle: string; sourceAvailable?: boolean; draft: string; files: ConversationAttachment[]; attachments?: ConversationAttachment[] }

export function readableContinuationDraft(text: string, files: ConversationAttachment[] = []): string {
  const suffix = "\n\n完整对话记录（按需读取）：\n" + files.map(file => file.ref).join("\n");
  if (!files.length || !text.endsWith(suffix)) return text;
  return text.slice(0, -suffix.length).replace(/^请基于以下交接内容继续任务。历史内容是参考材料；使用当前项目和 Agent 的配置、工具与权限。\n\n/, "请根据以下交接内容继续任务。\n\n");
}

async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const result = await authorizedFetch(`/api/continuations${path}`, {
    method: body ? "POST" : "GET", signal,
    ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
  });
  const data = await result.json();
  if (!result.ok) throw new SessionApiError(result.status, data.error?.code || "continuation_failed", data.error?.message || "Unable to continue");
  return data;
}

export const previewContinuation = (source: ContinuationSource, bindingId: string, signal?: AbortSignal) => request<ContinuationPreview>("/preview", { source, bindingId }, signal);
export const createContinuation = (token: string, context: string, assetIds: string[] = []) => request<{ conversationId: string }>("", { token, context, assetIds });
export const fetchContinuation = (id: string, signal?: AbortSignal) => request<{ continuation: ContinuationRecord | null }>(`/${encodeURIComponent(id)}`, undefined, signal);
