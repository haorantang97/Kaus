import { describe, expect, it, vi } from "vitest";
import { sessionRequest } from "./sessionApi";
import { connectEngine, fetchEngineCatalog } from "./enginePresets";

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
