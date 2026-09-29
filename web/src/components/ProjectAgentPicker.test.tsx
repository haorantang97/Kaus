import { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ProjectAgentPicker } from "./ProjectAgentPicker";
import type { BindingWire } from "../lib/sessionApi";
const api = vi.hoisted(() => ({ fetchConnectedBackends: vi.fn(), fetchProjectBindings: vi.fn(), ensureProjectBinding: vi.fn() }));
vi.mock("../lib/sessionApi", async original => ({ ...await original<object>(), ...api }));
const selected = vi.fn();
const binding = (project: string, backend = "codex") => ({ id: `binding:${project}:${backend}`, projectId: `project:${project}`, backendId: `backend:${backend}`, enabled: true, isDefault: false } as BindingWire);
function Picker({ project = "one", allowed }: { project?: string; allowed?: string[] }) {
  const [value, setValue] = useState<BindingWire | null>(null);
  return <ProjectAgentPicker projectId={`project:${project}`} value={value?.id} allowedBackendIds={allowed}
    onChange={row => { setValue(row); selected(row); }} />;
}
beforeEach(() => {
  vi.clearAllMocks();
  api.fetchConnectedBackends.mockResolvedValue({ backends: [
    { id: "backend:codex", productId: "codex", productName: "Codex", preferred: true },
    { id: "backend:hermes", productId: "hermes", productName: "Hermes", preferred: true },
    { id: "backend:hermes-acp", productId: "hermes", productName: "Hermes" },
  ] });
  api.fetchProjectBindings.mockResolvedValue({ bindings: [] });
  api.ensureProjectBinding.mockImplementation((pid: string, bid: string) => Promise.resolve(binding(pid.replace("project:", ""), bid.replace("backend:", ""))));
});
it("空项目可使用全局 Agent，首次选择自动建立关联", async () => {
  render(<Picker />);
  await waitFor(() => expect(screen.getByRole("button", { name: "引擎" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "引擎" }));
  expect(screen.getAllByRole("option", { name: "Hermes" })).toHaveLength(1);
  fireEvent.click(screen.getByRole("option", { name: "Codex" }));
  await waitFor(() => expect(selected).toHaveBeenLastCalledWith(binding("one")));
  expect(api.ensureProjectBinding).toHaveBeenCalledWith("project:one", "backend:codex");
});
it("旧关联停用时仍能主动选择全局 Agent，加载列表不会修改关联", async () => {
  api.fetchProjectBindings.mockResolvedValue({ bindings: [{ ...binding("one"), enabled: false }] });
  render(<Picker />);
  await waitFor(() => expect(screen.getByRole("button", { name: "引擎" })).toBeEnabled());
  expect(api.ensureProjectBinding).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "引擎" }));
  fireEvent.click(screen.getByRole("option", { name: "Codex" }));
  await waitFor(() => expect(api.ensureProjectBinding).toHaveBeenCalledWith("project:one", "backend:codex"));
});
it("切换项目后忽略旧项目尚未完成的选择", async () => {
  let finish!: (row: BindingWire) => void;
  api.ensureProjectBinding.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<Picker />);
  await waitFor(() => expect(screen.getByRole("button", { name: "引擎" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "引擎" }));
  fireEvent.click(screen.getByRole("option", { name: "Codex" }));
  view.rerender(<Picker project="two" />);
  await waitFor(() => expect(api.fetchProjectBindings).toHaveBeenCalledWith("project:two", expect.any(AbortSignal), { includeStatus: false }));
  await act(async () => finish(binding("one")));
  expect(selected).not.toHaveBeenCalledWith(binding("one"));
  expect(screen.getByRole("button", { name: "引擎" })).not.toHaveTextContent("Codex");
});
it("组长按适用连接筛选后合并产品，保留可用的备用协议", async () => {
  render(<Picker allowed={["backend:hermes-acp"]} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "引擎" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "引擎" }));
  expect(screen.queryByRole("option", { name: "Codex" })).toBeNull();
  fireEvent.click(screen.getByRole("option", { name: "Hermes" }));
  await waitFor(() => expect(api.ensureProjectBinding).toHaveBeenCalledWith("project:one", "backend:hermes-acp"));
});
