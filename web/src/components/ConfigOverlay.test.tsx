import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { ConfigOverlay } from "./ConfigOverlay";
import { apiGet } from "../lib/api";
import { archiveConversation, fetchArchivedConversations, fetchProjects } from "../lib/sessionApi";
vi.mock("../lib/api", () => ({ apiGet: vi.fn() }));
vi.mock("../lib/sessionApi", () => ({ archiveConversation: vi.fn(), fetchArchivedConversations: vi.fn(), fetchProjects: vi.fn() }));
vi.mock("./TerminalSettings", () => ({ TerminalSettings: () => <div>终端偏好</div> }));
vi.mock("./CoordinatorSettings", () => ({ CoordinatorSettings: () => <div>组长偏好</div> }));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchProjects).mockResolvedValue({ projects: [] } as never);
  vi.mocked(fetchArchivedConversations).mockResolvedValue({ conversations: [{ id: "conversation:one", title: "验收记录", projectId: "project:a", backendId: "backend:codex", updatedAt: "2026-09-27", archivedAt: "2026-09-27" }] });
  vi.mocked(archiveConversation).mockResolvedValue({ conversation: {} } as never);
  vi.mocked(apiGet).mockResolvedValue({ categories: [{ id: "model", label: "模型", desc: "不需要的解释", items: [{ k: "默认模型", v: "provider/model" }] }] });
});
it("设置按需读取原生配置，字段仍可展开查看", async () => {
  const user = userEvent.setup();
  render(<MemoryRouter><ConfigOverlay onClose={() => {}} /></MemoryRouter>);
  expect(screen.getByText("终端偏好")).toBeInTheDocument();
  expect(apiGet).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "原生配置" }));
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
  await user.selectOptions(await screen.findByRole("combobox", {name:"去项目页"}), "media");
  expect(screen.getByTestId("path")).toHaveTextContent("/projects/media");
  await user.click(screen.getByRole("button", {name:"已归档"}));
  await user.click(await screen.findByRole("button", {name:"查看 验收记录"}));
  expect(screen.getByTestId("path")).toHaveTextContent("/conversations/conversation%3Aone");
});
