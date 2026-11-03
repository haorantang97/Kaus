import { afterEach, expect, it, vi } from "vitest";
import { setLocalePref } from "../i18n";
import { deleteAgent } from "./api";
import { authorizedFetch } from "./authorizedFetch";

vi.mock("./authorizedFetch", () => ({ authorizedFetch: vi.fn() }));
afterEach(() => setLocalePref("zh"));

it("localizes a refused Project delete and keeps unknown details", async () => {
  const message = "此 Project 有对话正在运行，结束后再删除。";
  vi.mocked(authorizedFetch).mockResolvedValue(new Response(JSON.stringify({
    detail: message, error: { code: "project_has_active_runtime", message, activeConversationIds: ["conversation:x"] },
  }), { status: 409 }));
  setLocalePref("en");
  await expect(deleteAgent("design")).rejects.toThrow("A conversation is running. Stop it first.");
  vi.mocked(authorizedFetch).mockResolvedValue(new Response(JSON.stringify({ detail: "该 agent 还有下属" }), { status: 409 }));
  await expect(deleteAgent("design")).rejects.toThrow("该 agent 还有下属");
});
