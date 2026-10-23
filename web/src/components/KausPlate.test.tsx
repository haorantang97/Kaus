import { beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { KausPlate } from "./KausPlate";
import { buildConversationGroups, type SidebarConversation } from "../lib/conversationIndex";
import type { ProjectWire } from "../lib/sessionApi";
import { setLocalePref } from "../i18n";

/* ★M-1 + batch42（用户裁决：概览页取消）。
 *
 * 铭牌搬到草稿页顶上，右边那枚「新会话」去掉了——这一页本身就是新会话。
 * 副行仍旧载真数据：为零的段不写，项目数总在。 */

const project = (slug: string, displayName: string): ProjectWire => ({
  id: `project:${slug}`,
  slug,
  displayName,
  parentProjectId: null,
  workspaceRoot: null,
  status: "active",
});

const projects = [project("default", "X"), project("pronto", "Pronto"), project("atlas", "Atlas")];

const conversation = (
  id: string,
  slug: string,
  over: Partial<SidebarConversation> = {},
): SidebarConversation => ({
  id,
  projectId: `project:${slug}`,
  title: id,
  state: "idle",
  updatedAt: "2026-09-08T10:00:00Z",
  backendId: "backend:mock",
  ...over,
});

const renderPlate = (conversations: SidebarConversation[]) =>
  render(
    <KausPlate
      groups={buildConversationGroups(projects, conversations)}
      projectCount={projects.length}
    />,
  );

beforeEach(() => setLocalePref("zh"));

describe("铭牌（★M-1）", () => {
  it("副行写的是三个数，不是口号", () => {
    renderPlate([
      conversation("c1", "pronto", { state: "paused" }),
      conversation("c2", "pronto", { state: "running" }),
    ]);
    const line = screen.getByTestId("overview-plate-summary").textContent ?? "";
    expect(line).toContain("1 条等你处理");
    expect(line).toContain("1 条运行中");
    expect(line).toContain("3 个项目");
    expect(line).not.toMatch(/SELF-GOVERNING|THE APEX|EST\. 2026|CONTROL PLANE/);
  });

  it("为零的那一段不写；项目数总在（否则牌子会缺半截）", () => {
    renderPlate([conversation("c1", "pronto")]);
    const line = screen.getByTestId("overview-plate-summary").textContent ?? "";
    expect(line).not.toContain("等你处理");
    expect(line).not.toContain("运行中");
    expect(line).toBe("3 个项目");
  });

  it("batch42：牌子上没有「新会话」按钮——这一页本身就是新会话", () => {
    renderPlate([conversation("c1", "pronto")]);
    expect(screen.getByTestId("overview-plate").querySelectorAll("button")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "新会话" })).toBeNull();
    // 字标照旧在。
    expect(screen.getByText(/KAUS/)).toBeInTheDocument();
  });
});
