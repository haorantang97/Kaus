import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RuntimeVersions } from "./RuntimeVersions";
import { runtimeVersion } from "../lib/runtimeManagement";

vi.mock("../lib/runtimeManagement", () => ({ runtimeVersion: vi.fn() }));
beforeEach(() => { vi.clearAllMocks(); });
it("installs only on user action and uses the observed revision", async () => {
  const initial = { revision: "old", supported: true, available: false, version: null, recommendedVersion: "1.0.0", previousVersion: null };
  vi.mocked(runtimeVersion).mockResolvedValue(initial);
  render(<RuntimeVersions presetId="fixture" />);
  const button = await screen.findByRole("button", { name: "安装验证版本" });
  expect(runtimeVersion).toHaveBeenCalledTimes(1);
  vi.mocked(runtimeVersion).mockResolvedValue({ ...initial, available: true, version: "1.0.0", revision: "new", job: { state: "complete" } });
  await userEvent.click(button);
  expect(runtimeVersion).toHaveBeenCalledWith("fixture", "install", "old");
  await waitFor(() => expect(button).toBeDisabled());
});
it("preserves rollback controls and shows action failures", async () => {
  vi.mocked(runtimeVersion).mockResolvedValue({ revision: "current", supported: true, available: true, version: "2.0.0", recommendedVersion: "2.0.0", previousVersion: "1.0.0" });
  render(<RuntimeVersions presetId="fixture" />);
  const button = await screen.findByRole("button", { name: "回退适配器" });
  vi.mocked(runtimeVersion).mockRejectedValue(new Error("版本已变更"));
  await userEvent.click(button);
  expect(await screen.findByRole("alert")).toHaveTextContent("版本已变更");
  expect(button).toBeEnabled();
});
