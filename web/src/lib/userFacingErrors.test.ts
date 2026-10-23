import { afterEach, describe, expect, it } from "vitest";
import { setLocalePref } from "../i18n";
import { failureDiagnostic, failureSummary } from "./userFacingErrors";

afterEach(() => setLocalePref("zh"));
describe("public failure messages", () => {
  it("keeps validation readable without showing filesystem or transport diagnostics", () => {
    setLocalePref("zh");
    expect(failureSummary(new Error("名称不能为空"))).toBe("名称不能为空");
    expect(failureSummary(new Error("ENOENT: open /Users/example/private/session.jsonl"))).toBe("此内容暂时无法打开");
    expect(failureSummary(new Error("HTTP 500"))).toBe("操作未完成，请重试");
    expect(failureSummary(new Error('[{"loc":["body","field"],"msg":"invalid"}]'))).toBe("操作未完成，请重试");
    expect(failureSummary({ code: "driver_resume_unsupported", message: "driver_resume_unsupported" })).toBe("当前 Agent 不支持此操作");
    expect(failureSummary({ reason: "unknown" })).toBe("操作未完成，请重试");
  });
  it("preserves actionable permissions, authentication, conflicts and rate limits", () => {
    setLocalePref("zh");
    expect(failureSummary({ code: "auth_required", message: "Run /opt/bin/agent login" })).toBe("请登录此 Agent 后重试");
    expect(failureSummary({ status: 403, message: "Forbidden" })).toBe("没有操作权限，请检查权限设置");
    expect(failureSummary({ code: "revision_conflict", message: "conflict" })).toBe("内容已更新，请刷新后重试");
    expect(failureSummary({ status: 429, message: "HTTP 429" })).toBe("已达到使用限额，请稍后重试");
    setLocalePref("en");
    expect(failureSummary(new Error("Failed to fetch"))).toBe("Connection interrupted. Try again.");
  });
  it("removes terminal control sequences and secrets even in explicit details", () => {
    const value = "\u001b[31mENOENT\u001b[0m\u0000 /Users/demo/file token \nAuthorization: Bearer private-token api_key=private-key";
    expect(failureDiagnostic(value)).toBe("ENOENT /Users/demo/file token \nAuthorization: Bearer [redacted] api_key=[redacted]");
    expect(failureSummary("Invalid � output")).toBe("操作未完成，请重试");
  });
});
