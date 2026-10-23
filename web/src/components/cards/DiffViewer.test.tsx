import { expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DiffViewer, parseDiff, splitDiffLines } from "./DiffViewer";
const diff = "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -10,2 +10,2 @@\n same\n-old\n+new\n\\ No newline at end of file\n";
it("keeps real file headers, hunk line numbers and no-newline metadata", () => {
  const lines = parseDiff(diff);
  expect(lines.find(line => line.content === "old")).toMatchObject({ kind: "deletion", old: 11 });
  expect(lines.find(line => line.content === "new")).toMatchObject({ kind: "addition", next: 11 });
  expect(lines[0].raw).toContain("diff --git"); expect(lines.at(-1)?.raw).toContain("No newline");
});
it("switches aligned views and copies the unmodified diff", async () => {
  const user = userEvent.setup(); const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
  const { container } = render(<DiffViewer text={diff} />);
  await user.click(screen.getByRole("button", { name: "并排差异" }));
  expect(container.querySelectorAll(".kaus-diff-pair")).toHaveLength(2);
  expect(screen.getByText("new").closest(".kaus-diff-pair")).toBe(screen.getByText("old").closest(".kaus-diff-pair"));
  await user.click(screen.getByRole("button", { name: "复制差异" })); expect(write).toHaveBeenCalledWith(diff);
  write.mockRestore();
});
it("preserves existing row elements when streaming appends a new line", () => {
  const initial = "@@ -1,1 +1,1 @@\n context\n";
  const view = render(<DiffViewer text={initial} />); const row = screen.getByText("context");
  view.rerender(<DiffViewer text={initial + "+<script>not executable</script>"} />);
  expect(screen.getByText("context")).toBe(row);
  expect(screen.getByText("<script>not executable</script>")).toBeInTheDocument();
  expect(view.container.querySelector("script")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "统一差异" }));
});

it("aligns unequal replacements without losing blank lines or line numbers", () => {
  const rows = splitDiffLines(parseDiff("@@ -1,2 +1,1 @@\n-old\n-\n+new\n"));
  expect(rows).toEqual([
    { meta: "@@ -1,2 +1,1 @@" },
    { oldContent: "old", old: 1, nextContent: "new", next: 1, changed: true },
    { oldContent: "", old: 2, nextContent: undefined, next: undefined, changed: true },
  ]);
});
