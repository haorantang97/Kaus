import { describe, expect, it } from "vitest";
import { conversationPath, newConversationPath, parseRoute, projectPath, routeView, shellViewPath } from "./routes";

describe("路由解析", () => {
  /* 批次十一第 1 件：七个视图各有一个真 URL（IA §6.2）。 */
  it("七个视图各解析成自己那条路由", () => {
    expect(parseRoute("/")).toEqual({ name: "overview" });
    expect(parseRoute("/agents")).toEqual({ name: "agents" });
    expect(parseRoute("/dashboard")).toEqual({ name: "dash" });
    expect(parseRoute("/kanban")).toEqual({ name: "kanban" });
    expect(parseRoute("/vault")).toEqual({ name: "vault" });
    expect(parseRoute("/warehouse")).toEqual({ name: "warehouse" });
    expect(parseRoute("/config")).toEqual({ name: "config" });
  });

  it("视图 ↔ 路径互为逆运算，非视图路由 routeView 返回 null", () => {
    for (const view of ["overview", "agents", "dash", "kanban", "vault", "warehouse", "config"] as const) {
      expect(routeView(parseRoute(shellViewPath(view)))).toBe(view);
    }
    expect(routeView(parseRoute("/new"))).toBeNull();
    expect(routeView(parseRoute("/projects/pronto"))).toBeNull();
    expect(routeView(parseRoute("/nope"))).toBeNull();
  });

  it("四条路径各自解析正确（刷新与前进后退都靠它）", () => {
    expect(parseRoute("/")).toEqual({ name: "overview" });
    expect(parseRoute("/new")).toEqual({ name: "new", projectId: null });
    expect(parseRoute("/new", "?project=project:pronto")).toEqual({ name: "new", projectId: "project:pronto" });
    expect(parseRoute("/projects/pronto")).toEqual({ name: "project", projectRef: "pronto" });
    expect(parseRoute(conversationPath("conversation:abc"))).toEqual({
      name: "conversation",
      conversationId: "conversation:abc",
      after: null,
    });
  });

  it("对话页的 ?after= 只收非负整数，其余当没给（重放起点不能是垃圾值）", () => {
    expect(parseRoute("/conversations/c1", "?after=12")).toMatchObject({ after: 12 });
    expect(parseRoute("/conversations/c1", "?after=-3")).toMatchObject({ after: null });
    expect(parseRoute("/conversations/c1", "?after=abc")).toMatchObject({ after: null });
  });

  it("未知路径不静默跳首页，交给调用方处理", () => {
    expect(parseRoute("/nope/deep")).toEqual({ name: "unknown", pathname: "/nope/deep" });
  });

  it("构造函数与解析函数互为逆运算", () => {
    expect(parseRoute(projectPath("pronto"))).toEqual({ name: "project", projectRef: "pronto" });
    expect(parseRoute("/new", newConversationPath("project:x").split("?")[1] ? `?${newConversationPath("project:x").split("?")[1]}` : "")).toEqual({
      name: "new",
      projectId: "project:x",
    });
  });
});
