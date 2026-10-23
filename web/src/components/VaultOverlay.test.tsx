import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { VaultOverlay } from "./VaultOverlay";
import { apiGet } from "../lib/api";
import { nativeArchives } from "../lib/nativeArchivesApi";

vi.mock("../lib/api", () => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
vi.mock("../lib/nativeArchivesApi", () => ({ nativeArchives: { sources: vi.fn(), list: vi.fn(), scan: vi.fn() } }));

it("opens the existing notes library without offering or discovering external conversation history", async () => {
  vi.mocked(apiGet).mockResolvedValue({ exists: true, root: "/synthetic/notes", file_count: 1,
    tree: [{ type: "file", name: "brief.md", title: "Project brief", path: "brief.md", size: 24, mtime: 0 }] });
  render(<VaultOverlay onClose={() => {}} />);
  expect(await screen.findByText("Project brief")).toBeVisible();
  expect(apiGet).toHaveBeenCalledWith("/api/vault");
  expect(screen.queryByRole("tablist")).toBeNull();
  for (const name of ["关联历史", "恢复备份", "回收站"]) {
    expect(screen.queryByRole("button", { name })).toBeNull();
  }
  expect(nativeArchives.sources).not.toHaveBeenCalled();
  expect(nativeArchives.list).not.toHaveBeenCalled();
  expect(nativeArchives.scan).not.toHaveBeenCalled();
});
