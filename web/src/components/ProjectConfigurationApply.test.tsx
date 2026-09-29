import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ProjectConfigurationApply } from "./ProjectConfigurationApply";
const api = vi.hoisted(() => ({ materializeBinding: vi.fn() }));
vi.mock("../lib/sessionApi", async original => ({ ...await original<object>(), ...api }));
vi.mock("./ProjectAgentPicker", () => ({ ProjectAgentPicker: ({ onChange }: { onChange: (v: object) => void }) =>
  <button onClick={() => onChange({ id: "binding:demo:codex", backendId: "backend:codex" })}>Codex</button> }));
it("选择全局 Agent 后仅预览项目配置，不直接写入", async () => {
  api.materializeBinding.mockResolvedValue({ result: { applied: [], unsupported: [], warnings: [], dryRun: true }, dryRun: true });
  render(<ProjectConfigurationApply projectId="project:demo" />);
  fireEvent.click(screen.getByRole("button", { name: "应用项目配置" }));
  expect(screen.getByRole("button", { name: "预览变更" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Codex" }));
  fireEvent.click(screen.getByRole("button", { name: "预览变更" }));
  await waitFor(() => expect(api.materializeBinding).toHaveBeenCalledWith("binding:demo:codex", { confirm: false, adopt: false }));
  expect(api.materializeBinding).toHaveBeenCalledTimes(1);
});
