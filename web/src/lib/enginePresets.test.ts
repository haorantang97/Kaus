import { describe, expect, it, vi } from "vitest";
import { sessionRequest } from "./sessionApi";
import { connectEngine, fetchEngineCatalog, distinctEngineBindings, preferredEngineConnections } from "./enginePresets";
import { backendDisplay, bindingEngineLabel } from "./backend-display";

vi.mock("./sessionApi", () => ({ sessionRequest: vi.fn().mockResolvedValue({ engines: [] }) }));

describe("统一 Agent 目录接口", () => {
  it("目录查询复用会话鉴权并保留取消信号", async () => {
    const controller = new AbortController();
    await fetchEngineCatalog(controller.signal);
    expect(sessionRequest).toHaveBeenCalledWith("/api/engine-catalog", { signal: controller.signal });
  });
  it("接入只发送预设 ID，不提交命令或凭据", async () => {
    await connectEngine("alpha");
    expect(sessionRequest).toHaveBeenCalledWith("/api/engine-connections", { method: "POST", body: { presetId: "alpha" } });
  });
});

describe("产品名称与真实接入分离", () => {
  it("所有旧协议名称统一展示，同时保留用户配置名", () => {
    expect(backendDisplay("backend:hermes-acp").displayName).toBe("Hermes");
    expect(bindingEngineLabel("backend:hermes-acp", "Hermes (ACP)")).toBe("Hermes");
    expect(bindingEngineLabel("backend:hermes", "research")).toBe("Hermes · research");
    expect(backendDisplay("backend:deepseek-acp").displayName).toBe("DeepSeek Harness");
  });
  it("新建入口按后台推荐选择同产品的连接，已有选中绑定和独立配置不变", () => {
    const old = { id: "binding:http", backendId: "backend:hermes", displayName: "Hermes HTTP", projectId: "project:a", isDefault: true };
    const acp = { ...old, id: "binding:acp", backendId: "backend:hermes-acp", displayName: "Hermes (ACP)", isDefault: false };
    const named = { ...old, id: "binding:research", displayName: "Research" };
    expect(distinctEngineBindings([old, acp, named], acp.id).map(x => x.id)).toEqual([acp.id, named.id]);
    expect(preferredEngineConnections([
      { id: "backend:hermes-acp", productId: "hermes", probeState: "unknown" },
      { id: "backend:custom-http", productId: "hermes", probeState: "available", preferred: true },
    ]).map(x => x.id)).toEqual(["backend:custom-http"]);
    expect(old.backendId).toBe("backend:hermes");
  });
});
