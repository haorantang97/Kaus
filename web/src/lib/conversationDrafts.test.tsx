import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { conversationDraftKey, readConversationDraft, useConversationDraft } from "./conversationDrafts";
import { attachmentName } from "./attachmentsApi";

afterEach(() => vi.restoreAllMocks());

describe("conversation drafts", () => {
  it("restores text and uploaded attachments after remount, without mixing conversations", () => {
    const first = renderHook(() => useConversationDraft("one"));
    act(() => first.result.current.update({ text: "未发送的草稿", attachments: [{ kind: "file", ref: "file:///upload/note.txt", name: "note.txt" }] }));
    first.unmount();
    const second = renderHook(() => useConversationDraft("two"));
    expect(second.result.current.draft).toEqual({ text: "", attachments: [] });
    const restored = renderHook(() => useConversationDraft("one"));
    expect(restored.result.current.draft.text).toBe("未发送的草稿");
    expect(restored.result.current.draft.attachments[0].name).toBe("note.txt");
  });

  it("keeps an upload that completes after switching on its original conversation", () => {
    const hook = renderHook(({ id }) => useConversationDraft(id), { initialProps: { id: "one" } });
    const oldUpdate = hook.result.current.update;
    hook.rerender({ id: "two" });
    act(() => hook.result.current.update({ text: "第二个草稿" }));
    act(() => oldUpdate(current => ({ ...current, attachments: [{ kind: "file", ref: "file:///one/a.txt" }] })));
    expect(hook.result.current.draft.text).toBe("第二个草稿");
    expect(hook.result.current.draft.attachments).toEqual([]);
    hook.rerender({ id: "one" });
    expect(hook.result.current.draft.attachments[0].ref).toBe("file:///one/a.txt");
  });

  it("does not lose parallel upload results and removes the saved draft after send", () => {
    const hook = renderHook(() => useConversationDraft("one"));
    act(() => {
      hook.result.current.update(current => ({ ...current, attachments: [...current.attachments, { kind: "file", ref: "a" }] }));
      hook.result.current.update(current => ({ ...current, attachments: [...current.attachments, { kind: "file", ref: "b" }] }));
    });
    expect(hook.result.current.draft.attachments).toHaveLength(2);
    act(() => hook.result.current.update({ text: "", attachments: [] }));
    expect(localStorage.getItem(conversationDraftKey("one"))).toBeNull();
  });

  it("keeps editable text in memory if storage is unavailable and reports it", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
    const hook = renderHook(() => useConversationDraft("one"));
    act(() => hook.result.current.update({ text: "仍在输入框" }));
    expect(hook.result.current.draft.text).toBe("仍在输入框");
    expect(hook.result.current.storageFailed).toBe(true);
  });

  it("recovers corrupt storage and malformed encoded filenames without crashing", () => {
    localStorage.setItem(conversationDraftKey("one"), "invalid json");
    expect(readConversationDraft("one")).toEqual({ text: "", attachments: [] });
    expect(attachmentName({ kind: "file", ref: "file:///bad%ZZ.txt" })).toBe("bad%ZZ.txt");
  });
});
