import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { ErrorCard } from "./ErrorCard";

it("keeps raw codes and local paths in explicit details while preserving retry", async () => {
  render(<ErrorCard summary="ENOENT: /Users/example/private/record.jsonl" code="source_missing" />);
  expect(screen.getByText("此内容暂时无法打开")).toBeVisible();
  expect(screen.queryByText(/source_missing/)).not.toBeInTheDocument();
  expect(screen.queryByText(/\/Users\/example/)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "查看详情" }));
  expect(screen.getByText(/source_missing/)).toHaveTextContent("ENOENT");
});
