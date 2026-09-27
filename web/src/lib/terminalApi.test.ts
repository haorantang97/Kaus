import { expect, it, vi } from "vitest";
import { sessionRequest } from "./sessionApi";
import { fetchTerminalSettings } from "./terminalApi";
vi.mock("./sessionApi", () => ({ sessionRequest: vi.fn() }));
it.each([{}, null, { app: "cmux", launchers: {} }, { app: "cmux", launchers: [null] }])("rejects malformed terminal responses before rendering", async (response) => {
  vi.mocked(sessionRequest).mockResolvedValueOnce(response);
  await expect(fetchTerminalSettings()).rejects.toThrow("无法读取终端设置");
});
