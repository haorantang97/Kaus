import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { SelfCheckDialog } from "./SelfCheckDialog";
import { fetchSelfCheck, fetchSelfCheckOptions, markCapabilitiesBound, startSelfCheck } from "../lib/skillTreeApi";
import { fetchModelCatalog } from "../lib/sessionApi";

vi.mock("../lib/skillTreeApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/skillTreeApi")>(),
  fetchSelfCheckOptions: vi.fn(), startSelfCheck: vi.fn(), fetchSelfCheck: vi.fn(), markCapabilitiesBound: vi.fn(),
}));
vi.mock("../lib/sessionApi", async importOriginal => ({
  ...await importOriginal<typeof import("../lib/sessionApi")>(), fetchModelCatalog: vi.fn(),
}));

const run = { conversationId: "conversation:c1", agentKey: "codex", agentLabel: "Codex", summary: "", count: 2, suggestions: [] };

beforeEach(() => {
  vi.mocked(fetchSelfCheckOptions).mockReset().mockResolvedValue({ agentKey: "codex", agentLabel: "Codex", bindingId: "b1",
    backendId: "backend:codex", defaultModelId: null, count: 2 });
  vi.mocked(fetchModelCatalog).mockReset().mockResolvedValue({ bindingId: "b1", mode: "dynamic", defaultModelId: null,
    defaultProviderId: null, supportsReasoning: false, models: [
      { modelId: "small", displayName: "Small", providerId: null, contextWindow: null, reasoningLevels: [], availability: "available" },
      { modelId: "gone", displayName: "Gone", providerId: null, contextWindow: null, reasoningLevels: [], availability: "unavailable" }] });
  vi.mocked(startSelfCheck).mockReset().mockResolvedValue({ ...run, status: "running" });
  vi.mocked(fetchSelfCheck).mockReset().mockResolvedValue({ ...run, status: "answered", summary: "一条疑似绑定", suggestions: [
    { id: "s-render", kind: "skill", name: "codex-render", verdict: "likely", reason: "大量调用 Codex 专有工具" },
    { id: "codex:memory", kind: "memory", name: "Agent 自带记忆", verdict: "bound", reason: "存在 Codex 自己的状态里" }] });
  vi.mocked(markCapabilitiesBound).mockReset().mockResolvedValue({} as never);
});

describe("Agent 自查", () => {
  it("runs with the chosen model, lists each suggestion with its reason, and marks one bound on click", async () => {
    const user = userEvent.setup();
    const onMarked = vi.fn();
    render(<MemoryRouter><SelfCheckDialog agentKey="codex" agentLabel="Codex" onClose={() => {}} onMarked={onMarked} /></MemoryRouter>);
    expect(await screen.findByText("复核 2 项")).toBeVisible();
    expect(screen.getByText(/Agent 绑定能力只能由所属 Agent 使用/)).toBeVisible();
    const model = screen.getByRole("combobox", { name: "模型" });
    await waitFor(() => expect(screen.getByRole("option", { name: "Small" })).toBeInTheDocument());
    expect(screen.queryByRole("option", { name: "Gone" })).toBeNull();
    await user.selectOptions(model, "small");
    await user.click(screen.getByRole("button", { name: "开始自查" }));
    expect(startSelfCheck).toHaveBeenCalledWith("codex", "small");
    expect(await screen.findByRole("status")).toHaveTextContent("复核中");
    expect(await screen.findByText("一条疑似绑定", undefined, { timeout: 4000 })).toBeVisible();
    expect(screen.getByText("大量调用 Codex 专有工具")).toBeVisible();
    expect(screen.getByText("疑似")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "将 Agent 自带记忆 标为绑定" }));
    expect(markCapabilitiesBound).toHaveBeenCalledWith("codex", ["codex:memory"]);
    expect(await screen.findByText("已标为绑定")).toBeVisible();
    expect(onMarked).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "打开对话" })).toBeVisible();
  });
});
