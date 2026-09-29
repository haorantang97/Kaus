import { messageText, type TimelineItem } from "./timeline/reducer";

export interface ConversationJumpEntry { id: string; preview: string; answer?: string }

function excerpt(text: string, limit: number, paragraphs = false): string {
  const plain = text.slice(0, 2000).replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/<[^>]*>/g, "").replace(/^\s*(?:#{1,6}|>|[-*+]\s|\d+\.\s)\s*/gm, "")
    .replace(/[*`_~]/g, "").replace(/\r\n?/g, "\n");
  return (paragraphs ? plain.split(/\n\s*\n/).map(part => part.replace(/\s+/g, " ").trim()).filter(Boolean).join("\n\n")
    : plain.replace(/\s+/g, " ").trim()).slice(0, limit);
}

/** A local excerpt of each turn; reasoning and child-agent output stay out of the preview. */
export function conversationJumpEntries(items: TimelineItem[], attachmentLabel: string): ConversationJumpEntry[] {
  const entries: ConversationJumpEntry[] = [];
  let current: ConversationJumpEntry | null = null;
  let hasFinal = false;
  for (const item of items) {
    if (item.kind !== "message" || item.parentRunId) continue;
    if (item.role === "user") {
      const entry = { id: item.itemId, preview: excerpt(messageText(item), 220) || attachmentLabel };
      entries.push(entry);
      if (item.deliveryStatus !== "failed") { current = entry; hasFinal = false; }
    } else if (current) {
      const text = excerpt(messageText(item), 500, true);
      if (!text) continue;
      if (item.phase === "final_answer" || item.phase !== "commentary" || !hasFinal) current.answer = text;
      if (item.phase !== "commentary") hasFinal = true;
    }
  }
  return entries;
}
