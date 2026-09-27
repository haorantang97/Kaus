import { sessionRequest } from "./sessionApi";

export interface ConversationAttachment {
  kind: "file";
  ref: string;
  mimeType?: string | null;
  name?: string;
  size?: number;
}

export const ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;
export const ATTACHMENT_MAX_COUNT = 8;

export async function uploadConversationAttachment(
  conversationId: string,
  file: File,
): Promise<ConversationAttachment> {
  const data = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("File could not be read"));
    reader.onload = () => {
      const value = typeof reader.result === "string" ? reader.result : "";
      resolve(value.slice(value.indexOf(",") + 1));
    };
    reader.readAsDataURL(file);
  });
  return sessionRequest(`/api/conversations/${encodeURIComponent(conversationId)}/attachments`, {
    method: "POST",
    body: { name: file.name, mimeType: file.type || "application/octet-stream", data },
  });
}

export function attachmentName(attachment: ConversationAttachment): string {
  if (attachment.name) return attachment.name;
  const leaf = attachment.ref.split("/").pop() || "file";
  try { return decodeURIComponent(leaf); }
  catch { return leaf; }
}

export function fileSizeLabel(bytes: number): string {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.ceil(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
