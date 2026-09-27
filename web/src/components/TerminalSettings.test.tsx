import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TerminalSettings } from "./TerminalSettings";
import { fetchTerminalSettings, saveTerminalSettings } from "../lib/terminalApi";
vi.mock("../lib/terminalApi", () => ({ fetchTerminalSettings: vi.fn(), saveTerminalSettings: vi.fn() }));
const launchers = [
  { id: "cmux", label: "cmux", installed: true },
  { id: "terminal", label: "Terminal", installed: true },
  { id: "iterm2", label: "iTerm2", installed: false },
];
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchTerminalSettings).mockResolvedValue({ app: "cmux", launchers });
  vi.mocked(saveTerminalSettings).mockImplementation(async (app) => ({ app, launchers }));
});
it("保存默认启动器，并在下次打开设置时读回", async () => {
  const user = userEvent.setup();
  const view = render(<TerminalSettings />);
  const select = await screen.findByRole("combobox", { name: "默认终端" });
  await waitFor(() => expect(select).toHaveValue("cmux"));
  expect(screen.getByRole("option", { name: "iTerm2 · 未安装" })).toBeDisabled();
  await user.selectOptions(select, "terminal");
  await waitFor(() => expect(saveTerminalSettings).toHaveBeenCalledWith("terminal"));
  expect(await screen.findByRole("status", { name: "已保存" })).toBeInTheDocument();
  view.unmount();
  vi.mocked(fetchTerminalSettings).mockResolvedValue({ app: "terminal", launchers });
  render(<TerminalSettings />);
  await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("terminal"));
});
it("保存失败时保留已确认的选择", async () => {
  const user = userEvent.setup();
  vi.mocked(saveTerminalSettings).mockRejectedValue(new Error("无法保存终端偏好"));
  render(<TerminalSettings />);
  await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("cmux"));
  await user.selectOptions(screen.getByRole("combobox"), "terminal");
  expect(await screen.findByRole("alert")).toHaveTextContent("无法保存终端偏好");
  expect(screen.getByRole("combobox")).toHaveValue("cmux");
});
