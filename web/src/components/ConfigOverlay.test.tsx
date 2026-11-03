import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { ConfigOverlay } from "./ConfigOverlay";
import { apiGet, LegacyApiError, restoreDeletedAgent } from "../lib/api";
import { archiveConversation, deleteConversation, fetchArchivedConversations, fetchProjects, fetchConnectedBackends, sessionRequest } from "../lib/sessionApi";
vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { apiGet: vi.fn(), restoreDeletedAgent: vi.fn(), LegacyApiError: actual.LegacyApiError };
});
vi.mock("./NewAgentModal", () => ({ NewAgentModal: ({ initialDisplay, initialId, onCreated }: { initialDisplay: string; initialId: string; onCreated: (name: string) => void }) =>
  <button type="button" onClick={() => onCreated(initialId)}>新建 {initialDisplay} {initialId}</button> }));
vi.mock("./MoveConversationDialog", () => ({ MoveConversationDialog: ({ item, initialProjectId }: { item: { title: string }; initialProjectId?: string }) =>
  <div role="dialog">移动 {item.title} {initialProjectId ?? ""}</div> }));
vi.mock("../lib/sessionApi", () => ({ archiveConversation: vi.fn(), deleteConversation: vi.fn(), describeFailure: vi.fn((failure: unknown) => String(failure)), fetchArchivedConversations: vi.fn(), fetchProjects: vi.fn(), fetchConnectedBackends: vi.fn(), sessionRequest: vi.fn() }));
vi.mock("./TerminalSettings", () => ({ TerminalSettings: () => <div>终端偏好</div> }));
vi.mock("./CoordinatorSettings", () => ({ CoordinatorSettings: () => <div>组长偏好</div> }));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchProjects).mockResolvedValue({ projects: [] } as never);
  vi.mocked(fetchConnectedBackends).mockResolvedValue({ backends: [], count: 0 });
  vi.mocked(fetchArchivedConversations).mockResolvedValue({ conversations: [{ id: "conversation:one", title: "验收记录", projectId: "project:a", backendId: "backend:codex", updatedAt: "2026-09-27", archivedAt: "2026-09-27" }] });
  vi.mocked(archiveConversation).mockResolvedValue({ conversation: {} } as never);
});
it("设置顶栏没有手动刷新，后台推送刷新 Agent 连接且不离开分类", async () => {
  const user = userEvent.setup();
  const view = render(<MemoryRouter><ConfigOverlay onClose={() => {}} refreshKey={0} /></MemoryRouter>);
  expect(screen.queryByRole("button", { name: "重新载入" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Agent" }));
  await waitFor(() => expect(fetchConnectedBackends).toHaveBeenCalledTimes(1));
  view.rerender(<MemoryRouter><ConfigOverlay onClose={() => {}} refreshKey={1} /></MemoryRouter>);
  await waitFor(() => expect(fetchConnectedBackends).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("button", { name: "Agent" })).toHaveAttribute("aria-current", "page");
});
it("返回系统授权或收到后台刷新时保留已展开的引导", async () => {
  vi.mocked(sessionRequest).mockResolvedValue({enabled:true,setup:{status:"ready",permissions:{accessibility:true,"screen-recording":true}},agents:[],fallbacks:[],pendingResources:[]});
  const user = userEvent.setup();
  const view = render(<MemoryRouter><ConfigOverlay onClose={() => {}} refreshKey={0}/></MemoryRouter>);
  await user.click(screen.getByRole("button", {name:"Agent"}));
  const details=screen.getByText("电脑操作").closest("details")!;
  details.open=true;fireEvent(details,new Event("toggle"));
  await user.click(await screen.findByRole("button", {name:"授权"}));
  view.rerender(<MemoryRouter><ConfigOverlay onClose={() => {}} refreshKey={1}/></MemoryRouter>);
  await waitFor(()=>expect(sessionRequest).toHaveBeenLastCalledWith("/api/computer-use/permissions",{signal:expect.any(AbortSignal)}));
  expect(screen.getByText("电脑操作").closest("details")).toBe(details);
  expect(details).toHaveAttribute("open");
  expect(screen.getByRole("region",{name:"电脑操作授权"})).toBeVisible();
  expect(screen.getAllByText("已开启")).toHaveLength(2);
});
it("设置不再提供运行概况或只读原生配置", () => {
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  expect(screen.getByText("终端偏好")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "高级" })).toBeNull();
  expect(screen.queryByText("运行概况")).toBeNull();
  expect(screen.queryByText("原生配置")).toBeNull();
  expect(apiGet).not.toHaveBeenCalled();
});
it("恢复成功才从归档列表移除对话", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "已归档" }));
  await user.click(await screen.findByRole("button", { name: "恢复 验收记录" }));
  await waitFor(() => expect(archiveConversation).toHaveBeenCalledWith("conversation:one", false));
  expect(await screen.findByText("暂无归档")).toBeInTheDocument();
});
it("通用页不提供项目跳转；归档入口进入目标页，不被关闭回调带回首页", async () => {
  vi.mocked(fetchProjects).mockResolvedValue({ projects: [{id:"project:media",slug:"media",displayName:"Media"}] } as never);
  function Shell() {
    const navigate = useNavigate();
    const location = useLocation();
    return <><ConfigOverlay onClose={() => navigate("/")} /><output data-testid="path">{location.pathname}</output></>;
  }
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/config"]}><Shell /></MemoryRouter>);
  await waitFor(() => expect(fetchProjects).toHaveBeenCalled());
  expect(screen.queryByRole("combobox", {name:"去项目页"})).toBeNull();
  expect(screen.queryByText("项目设置")).toBeNull();
  await user.click(screen.getByRole("button", {name:"已归档"}));
  await user.click(await screen.findByRole("button", {name:"查看 验收记录"}));
  expect(screen.getByTestId("path")).toHaveTextContent("/conversations/conversation%3Aone");
});

it("账号额度只在进入对应分类时读取", async () => {
  const user = userEvent.setup();
  const quota = await import("../lib/accountQuotasApi");
  const fetch = vi.spyOn(quota, "fetchAccountQuotas").mockResolvedValue({accounts:[],unavailable:[],updatedAt:"2026-10-17T00:00:00Z"});
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  expect(fetch).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", {name:"账号额度"}));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(await screen.findByText("暂无可显示的额度")).toBeInTheDocument();
});
it("通用设置可修改外观和语言", async () => {
  const change = vi.fn();
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} theme="light" onThemeChange={change} /></MemoryRouter>);
  await user.click(screen.getByRole("combobox", {name:"外观"}));
  await user.click(screen.getByRole("option", {name:"深色"}));
  expect(change).toHaveBeenCalledWith("dark");
  expect(screen.getByRole("combobox", {name:"语言"})).toBeInTheDocument();
});
it("关于页给出许可证、源码和第三方声明入口", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "关于" }));
  expect(screen.getByRole("link", { name: /许可证.*AGPL-3\.0-only/ })).toHaveAttribute("href", "https://github.com/haorantang97/Kaus/blob/main/LICENSE");
  expect(screen.getByRole("link", { name: /源代码/ })).toHaveAttribute("href", "https://github.com/haorantang97/Kaus");
  expect(screen.getByRole("link", { name: /第三方声明/ })).toHaveAttribute("href", "/THIRD_PARTY_NOTICES.txt");
});
it("已删除 Project 的归档对话保留原名称，可查看或手动删除", async () => {
  vi.mocked(fetchProjects).mockResolvedValue({ projects: [
    { id: "project:a", slug: "a", displayName: "Design", status: "disabled" },
    { id: "project:b", slug: "b", displayName: "Media", status: "active" },
  ] } as never);
  vi.mocked(deleteConversation).mockResolvedValue({ conversationId: "conversation:one", deleted: true });
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  expect(fetchProjects).toHaveBeenCalledWith(undefined, { includeDisabled: true });
  await user.click(screen.getByRole("button", { name: "已归档" }));
  expect(await screen.findByText(/Design · 已删除的 Project/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "恢复 验收记录" })).toBeNull();
  expect(screen.getByRole("button", { name: "恢复 Project 验收记录" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "查看 验收记录" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "删除 验收记录" }));
  expect(deleteConversation).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "确认删除 验收记录" }));
  await waitFor(() => expect(deleteConversation).toHaveBeenCalledWith("conversation:one"));
  expect(await screen.findByText("暂无归档")).toBeInTheDocument();
});
it("项目记录缺失时归档行标为已删除的 Project", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "已归档" }));
  expect(await screen.findByText(/^已删除的 Project/)).toBeInTheDocument();
});

const deletedProject = () => vi.mocked(fetchProjects).mockResolvedValue({ projects: [
  { id: "project:a", slug: "a", displayName: "Design", status: "disabled", parentProjectId: "project:root" },
  { id: "project:b", slug: "b", displayName: "Media", status: "active" },
] } as never);
it("恢复 Project 原位恢复后只恢复所点的对话", async () => {
  deletedProject();
  vi.mocked(restoreDeletedAgent).mockImplementation(async () => {
    vi.mocked(fetchArchivedConversations).mockResolvedValue({ conversations: [] });
    return { ok: true, restored: "a", label: "Design", parent: "root" };
  });
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "已归档" }));
  await user.click(await screen.findByRole("button", { name: "恢复 Project 验收记录" }));
  await waitFor(() => expect(archiveConversation).toHaveBeenCalledWith("conversation:one", false));
  expect(restoreDeletedAgent).toHaveBeenCalledWith("a", "root");
  expect(await screen.findByText("暂无归档")).toBeInTheDocument();
});
it("原位置不可用时打开预填的新建 Project，同名创建后恢复对话", async () => {
  deletedProject();
  vi.mocked(restoreDeletedAgent).mockRejectedValue(new LegacyApiError("原上级 Project 已不存在", 409, "project_parent_missing"));
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "已归档" }));
  await user.click(await screen.findByRole("button", { name: "恢复 Project 验收记录" }));
  await user.click(await screen.findByRole("button", { name: "新建 Design a" }));
  await waitFor(() => expect(archiveConversation).toHaveBeenCalledWith("conversation:one", false));
});
it("原位置已变动时同样打开预填的新建 Project", async () => {
  deletedProject();
  vi.mocked(restoreDeletedAgent).mockRejectedValue(new LegacyApiError("原位置已变动", 409, "project_anchor_moved"));
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "已归档" }));
  await user.click(await screen.findByRole("button", { name: "恢复 Project 验收记录" }));
  expect(await screen.findByRole("button", { name: "新建 Design a" })).toBeInTheDocument();
});
it("移到其他 Project 打开移动对话框", async () => {
  deletedProject();
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "已归档" }));
  await user.click(await screen.findByRole("button", { name: "移到其他 Project 验收记录" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("移动 验收记录");
});
