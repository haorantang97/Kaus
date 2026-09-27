import { useCallback, useRef, useState } from "react";
import type { ConversationAttachment } from "./attachmentsApi";

export interface ConversationDraft {
  text: string;
  attachments: ConversationAttachment[];
}

export const conversationDraftKey = (id: string) => `kaus.conversation-draft.v1.${id}`;

export function readConversationDraft(id: string): ConversationDraft {
  try {
    const row = JSON.parse(localStorage.getItem(conversationDraftKey(id)) || "null");
    if (!row || typeof row.text !== "string") return { text: "", attachments: [] };
    const attachments = Array.isArray(row.attachments)
      ? row.attachments.filter((a: unknown): a is ConversationAttachment => !!a && typeof a === "object" && (a as ConversationAttachment).kind === "file" && typeof (a as ConversationAttachment).ref === "string").slice(0, 8)
      : [];
    return { text: row.text, attachments };
  } catch {
    return { text: "", attachments: [] };
  }
}

export function writeConversationDraft(id: string, draft: ConversationDraft): boolean {
  try {
    if (!draft.text && !draft.attachments.length) localStorage.removeItem(conversationDraftKey(id));
    else localStorage.setItem(conversationDraftKey(id), JSON.stringify(draft));
    return true;
  } catch {
    return false;
  }
}

/** Persist only text and already-uploaded references, never binary contents. */
export function useConversationDraft(id: string) {
  const drafts = useRef(new Map<string, ConversationDraft>());
  const failures = useRef(new Set<string>());
  const activeId = useRef(id);
  activeId.current = id;
  const [, rerender] = useState(0);
  if (!drafts.current.has(id)) drafts.current.set(id, readConversationDraft(id));
  const update = useCallback((patch: Partial<ConversationDraft> | ((current: ConversationDraft) => ConversationDraft)) => {
    const base = drafts.current.get(id) ?? readConversationDraft(id);
    const draft = typeof patch === "function" ? patch(base) : { ...base, ...patch };
    drafts.current.set(id, draft);
    if (writeConversationDraft(id, draft)) failures.current.delete(id);
    else failures.current.add(id);
    // A slow upload keeps its original conversation even after the user switches.
    if (activeId.current === id) rerender(value => value + 1);
  }, [id]);
  return { draft: drafts.current.get(id)!, update, storageFailed: failures.current.has(id) };
}
