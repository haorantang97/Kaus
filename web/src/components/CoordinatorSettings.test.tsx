import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CoordinatorSettings } from "./CoordinatorSettings";
import { GroupRunContent, publicGroupText } from "./GroupRunContent";
import type { GroupWire } from "../lib/groupsApi";
const api = vi.hoisted(() => ({ sessionRequest: vi.fn(), fetchProjects: vi.fn(), fetchProjectBindings: vi.fn(), fetchModelCatalog: vi.fn(), fetchEffectiveSettings: vi.fn(), fetchEventsSnapshot: vi.fn(), resolveInteraction: vi.fn() }));
vi.mock("../lib/sessionApi", async original => ({ ...await original<object>(), ...api }));
vi.mock("../lib/groupStore", () => ({ refreshGroups: vi.fn().mockResolvedValue(undefined) }));
const group = { id: "g1", title: "讨论", status: "active", settings: {}, coordinatorEnabled: true } as GroupWire;
beforeEach(() => {
  vi.clearAllMocks();
  api.sessionRequest.mockImplementation((path: string) => Promise.resolve(path.endsWith("/options") ? { bindings: [{ id: "b1", projectId: "p1", projectName: "项目", displayName: "引擎", backendId: "mock", enabled: true }] } : { config: { bindingId: "b1", modelId: "same", providerId: "vendor-a", revision: 2 } }));
  api.fetchProjects.mockResolvedValue({ projects: [{ id: "p1", displayName: "项目" }] });
  api.fetchProjectBindings.mockResolvedValue({ bindings: [{ id: "b1", projectId: "p1", displayName: "引擎", backendId: "mock", enabled: true }] });
  api.fetchModelCatalog.mockResolvedValue({ models: ["vendor-a", "vendor-b"].map(providerId => ({ modelId: "same", providerId, providerLabel: providerId, displayName: providerId + " 模型", reasoningLevels: ["low", "high"] })) });
  api.fetchEffectiveSettings.mockResolvedValue({ reasoningEffort: { levels: ["low", "high"] }, workspaceRoot: { value: "/tmp" }, conversationControls: { reasoning: true, approvalModes: ["ask"] } });
  api.fetchEventsSnapshot.mockResolvedValue({ events: [], lastSequence: 0, truncated: false });
});
describe("独立组长", () => {
  it("原生无模型目录时保存引擎默认，并使用其实际推理档位", async () => {
    api.sessionRequest.mockImplementation((path: string) => Promise.resolve(path.endsWith("/options")
      ? { bindings: [{ id: "b1", projectId: "p1", projectName: "项目", displayName: "引擎", backendId: "mock", enabled: true }] }
      : { config: { bindingId: "b1", modelId: null, revision: 2 } }));
    api.fetchModelCatalog.mockResolvedValue({ models: [], engineDefaultAvailable: true, degraded: false });
    api.fetchEffectiveSettings.mockResolvedValue({ reasoningEffort: { levels: [] }, workspaceRoot: { value: "/tmp" },
      conversationControls: { reasoning: true, reasoningLevels: ["off", "medium"], approvalModes: [] } });
    render(<CoordinatorSettings group={group} />);
    fireEvent.click(screen.getByRole("button", { name: "组长配置" }));
    await screen.findByText("引擎默认");
    fireEvent.click(screen.getByRole("button", { name: "推理强度" }));
    fireEvent.click(screen.getByText("medium"));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.sessionRequest).toHaveBeenCalledWith("/api/groups/g1/coordinator", expect.objectContaining({
      method: "PUT", body: expect.objectContaining({ config: expect.objectContaining({ modelId: null, reasoningMode: "medium" }) }) })));
  });
  it("目录或登录失败不会伪装成引擎默认", async () => {
    api.fetchModelCatalog.mockResolvedValue({ models: [], engineDefaultAvailable: true, degraded: true, diagnostics: ["需要先登录引擎"] });
    render(<CoordinatorSettings group={group} />);
    fireEvent.click(screen.getByRole("button", { name: "组长配置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("需要先登录引擎");
    expect(screen.queryByText("引擎默认")).toBeNull();
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "设为默认" })).toBeDisabled();
  });
  it("运行模式与推理和权限分别保存", async () => {
    api.fetchEffectiveSettings.mockResolvedValue({ reasoningEffort: { levels: ["low", "high"] }, workspaceRoot: { value: "/tmp" },
      conversationControls: { reasoning: true, approvalModes: [], executionModes: [{ id: "code", name: "Code" }, { id: "debug", name: "Debug" }] } });
    render(<CoordinatorSettings group={group} />);
    fireEvent.click(screen.getByRole("button", { name: "组长配置" }));
    await screen.findByText("vendor-a 模型");
    fireEvent.click(await screen.findByRole("button", { name: "运行模式" }));
    fireEvent.click(screen.getByText("Debug"));
    fireEvent.click(screen.getByRole("button", { name: "推理强度" }));
    fireEvent.click(screen.getByText("high"));
    expect(screen.queryByRole("button", { name: "权限" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.sessionRequest).toHaveBeenCalledWith("/api/groups/g1/coordinator", expect.objectContaining({
      method: "PUT", body: expect.objectContaining({ config: expect.objectContaining({ executionMode: "debug", reasoningMode: "high" }) }) })));
  });
  it("同名模型以厂家身份保存，配置入口保持紧凑", async () => {
    render(<CoordinatorSettings group={group} />);
    fireEvent.click(screen.getByRole("button", { name: "组长配置" }));
    await screen.findByText("vendor-a 模型");
    fireEvent.click(screen.getByRole("button", { name: "模型" }));
    fireEvent.click(screen.getByText("vendor-b"));
    fireEvent.click(await screen.findByText("vendor-b 模型"));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.sessionRequest).toHaveBeenCalledWith("/api/groups/g1/coordinator", expect.objectContaining({ method: "PUT", body: expect.objectContaining({ expectedRevision: 2, config: expect.objectContaining({ modelId: "same", providerId: "vendor-b" }) }) })));
  });
  it("隐藏完整和逐字到达的控制协议", () => {
    expect(publicGroupText("答案\n```kaus-control\n{}\n```" )).toBe("答案");
    expect(publicGroupText("答案\n```kaus-")).toBe("答案");
    expect(publicGroupText("示例\n```python\nx=1\n```" )).toContain("python");
  });
  it("历史执行记录分页重放，正文不重复且不显示内部输入", async () => {
    const env = (sequence: number, event: object) => ({ sequence, event, conversationId: "c1", runId: "r1", eventId: `e${sequence}`, source: {} });
    api.fetchEventsSnapshot.mockResolvedValueOnce({ events: [env(1, { type: "message.started", messageId: "m1", role: "assistant" }), env(2, { type: "reasoning.delta", messageId: "m1", text: "核对结果与证据" })], lastSequence: 2, truncated: true })
      .mockResolvedValueOnce({ events: [env(3, { type: "message.completed", messageId: "m1", role: "assistant", text: "内部控制" }), env(4, { type: "terminal.started", terminalId: "t1", command: "verify" }), env(5, { type: "terminal.completed", terminalId: "t1", output: "passed", exitCode: 0 })], lastSequence: 5, truncated: false });
    render(<GroupRunContent conversationId="c1" runId="r1" />);
    await waitFor(() => expect(api.fetchEventsSnapshot).toHaveBeenCalledWith("c1", 2));
    expect(screen.queryByText("内部控制")).toBeNull();
    expect(await screen.findByTestId("terminal-row")).toBeInTheDocument();
    expect(await screen.findByTestId("reasoning-row")).toBeInTheDocument();
  });
});
