import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ProjectMemoryCard } from "./ProjectMemoryCard";

const api = vi.hoisted(() => ({ fetchProjectMemories: vi.fn(), patchProjectMemory: vi.fn(), createProjectMemory: vi.fn(),
  deleteProjectMemory: vi.fn(), fetchMemoryHistory: vi.fn(), undoMemoryChange: vi.fn() }));
vi.mock("../lib/sessionApi", async importOriginal => ({ ...(await importOriginal<typeof import("../lib/sessionApi")>()), ...api }));

const entry = (id: string, projectId: string, topic: string, body: string, kind: "note" | "overview" = "note") => ({
  id, projectId, kind, topic, body, status: "active", source: "backend:codex", createdAt: "2026-11-09T00:00:00Z", updatedAt: "2026-11-09T00:00:00Z" });

beforeEach(() => {
  Object.values(api).forEach(fn => fn.mockReset());
  api.fetchProjectMemories.mockResolvedValue({ overLimit: false, memories: [
    entry("m-root", "project:root", "包管理", "统一用 pnpm"),
    entry("m-own", "project:site", "设计", "首页主色是墨绿"),
    entry("m-ov", "project:site", "项目概述", "官网改版", "overview"),
  ] });
});

describe("项目记忆卡片", () => {
  it("概述在最上；上级条目只读，本级可以编辑", async () => {
    const user = userEvent.setup();
    api.patchProjectMemory.mockResolvedValue({});
    render(<ProjectMemoryCard projectId="project:site" projectLabel={id => id === "project:root" ? "Root" : id} />);
    const list = await screen.findByTestId("project-memory");
    const rows = within(list).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("官网改版");
    const inherited = rows.find(row => row.textContent?.includes("pnpm"))!;
    expect(inherited).toHaveTextContent("继承自 Root");
    expect(within(inherited).queryByRole("button", { name: "记忆操作" })).toBeNull();
    const own = rows.find(row => row.textContent?.includes("墨绿"))!;
    await user.click(within(own).getByRole("button", { name: "记忆操作" }));
    await user.click(screen.getByRole("menuitem", { name: "编辑" }));
    const box = screen.getByRole("textbox", { name: "内容" });
    await user.clear(box);
    await user.type(box, "首页主色改成深蓝");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.patchProjectMemory).toHaveBeenCalledWith("project:site", "m-own", { topic: "设计", body: "首页主色改成深蓝" }));
  });

  it("改动历史可以逐条撤销", async () => {
    const user = userEvent.setup();
    api.fetchMemoryHistory.mockResolvedValue({ history: [{ id: "h1", memoryId: "m-own", action: "update", before: null,
      after: entry("m-own", "project:site", "设计", "x"), actor: "backend:codex", at: "2026-11-09T00:00:00Z" }] });
    api.undoMemoryChange.mockResolvedValue({});
    render(<ProjectMemoryCard projectId="project:site" projectLabel={id => id} />);
    await screen.findByTestId("project-memory");
    await user.click(screen.getByRole("button", { name: "改动历史" }));
    const history = await screen.findByTestId("memory-history");
    expect(history).toHaveTextContent("修改 · 设计");
    await user.click(within(history).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(api.undoMemoryChange).toHaveBeenCalledWith("project:site", "h1"));
  });

  it("临时条目标出到期日；导入、合并与到期收起写明来源", async () => {
    const user = userEvent.setup();
    api.fetchProjectMemories.mockResolvedValue({ overLimit: false, memories: [
      { ...entry("m-tmp", "project:site", "本周", "迁移登录页"), type: "temporary", expiresAt: "2026-11-25T08:00:00Z" },
      { ...entry("m-imp", "project:site", "约定", "提交前跑测试"), source: "import:codex", type: "fact", expiresAt: null },
    ] });
    api.fetchMemoryHistory.mockResolvedValue({ history: [
      { id: "h2", memoryId: "m-old", action: "expire", before: null, after: entry("m-old", "project:site", "冻结", "x"), actor: "kaus", at: "2026-11-09T00:00:00Z" },
      { id: "h1", memoryId: "m-imp", action: "merge", before: null, after: entry("m-imp", "project:site", "约定", "x"), actor: "import:codex", at: "2026-11-09T00:00:00Z" },
    ] });
    render(<ProjectMemoryCard projectId="project:site" projectLabel={id => id} />);
    const rows = within(await screen.findByTestId("project-memory")).getAllByRole("listitem");
    expect(rows.find(row => row.textContent?.includes("迁移登录页"))).toHaveTextContent(/临时 · 至 11\/2[45]/);
    expect(rows.find(row => row.textContent?.includes("提交前跑测试"))).toHaveTextContent("从 Codex 导入");
    await user.click(screen.getByRole("button", { name: "改动历史" }));
    const history = await screen.findByTestId("memory-history");
    expect(history).toHaveTextContent("到期收起 · 冻结 · Kaus");
    expect(history).toHaveTextContent("合并 · 约定 · 从 Codex 导入");
  });
});
