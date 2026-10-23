import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { ConfigOverlay } from "./ConfigOverlay";
import { apiGet } from "../lib/api";
import { archiveConversation, fetchArchivedConversations, fetchProjects, fetchConnectedBackends, sessionRequest } from "../lib/sessionApi";
vi.mock("../lib/api", () => ({ apiGet: vi.fn() }));
vi.mock("../lib/sessionApi", () => ({ archiveConversation: vi.fn(), fetchArchivedConversations: vi.fn(), fetchProjects: vi.fn(), fetchConnectedBackends: vi.fn(), sessionRequest: vi.fn() }));
vi.mock("./TerminalSettings", () => ({ TerminalSettings: () => <div>终端偏好</div> }));
vi.mock("./CoordinatorSettings", () => ({ CoordinatorSettings: () => <div>组长偏好</div> }));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchProjects).mockResolvedValue({ projects: [] } as never);
  vi.mocked(fetchConnectedBackends).mockResolvedValue({ backends: [], count: 0 });
  vi.mocked(fetchArchivedConversations).mockResolvedValue({ conversations: [{ id: "conversation:one", title: "验收记录", projectId: "project:a", backendId: "backend:codex", updatedAt: "2026-09-27", archivedAt: "2026-09-27" }] });
  vi.mocked(archiveConversation).mockResolvedValue({ conversation: {} } as never);
  vi.mocked(apiGet).mockResolvedValue({ categories: [{ id: "model", label: "模型", desc: "不需要的解释", items: [{ k: "默认模型", v: "provider/model" }] }] });
});
it("设置顶栏刷新会重新读取 Agent 连接，无需离开分类", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "Agent" }));
  await waitFor(() => expect(fetchConnectedBackends).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("button", { name: "重新载入" }));
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
it("设置按需读取原生配置，字段仍可展开查看", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  expect(screen.getByText("终端偏好")).toBeInTheDocument();
  expect(apiGet).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "高级" }));
  await user.click(screen.getByText("原生配置"));
  await user.click(await screen.findByText("模型"));
  expect(screen.getByText("provider/model")).toBeVisible();
  expect(screen.queryByText("不需要的解释")).toBeNull();
});
it("恢复成功才从归档列表移除对话", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "已归档" }));
  await user.click(await screen.findByRole("button", { name: "恢复 验收记录" }));
  await waitFor(() => expect(archiveConversation).toHaveBeenCalledWith("conversation:one", false));
  expect(await screen.findByText("暂无归档")).toBeInTheDocument();
});
it("设置中的项目和归档入口进入目标页，不被关闭回调带回首页", async () => {
  vi.mocked(fetchProjects).mockResolvedValue({ projects: [{id:"project:media",slug:"media",displayName:"Media"}] } as never);
  function Shell() {
    const navigate = useNavigate();
    const location = useLocation();
    return <><ConfigOverlay onClose={() => navigate("/")} /><output data-testid="path">{location.pathname}</output></>;
  }
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/config"]}><Shell /></MemoryRouter>);
  await user.click(await screen.findByRole("combobox", {name:"去项目页"}));
  await user.type(screen.getByRole("searchbox"), "media");
  await user.click(screen.getByRole("option", {name:"Media"}));
  expect(screen.getByTestId("path")).toHaveTextContent("/projects/media");
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
