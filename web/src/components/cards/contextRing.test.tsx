import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ContextRing, UsagePill } from "./UsageBar";
import type { UsageSnapshot } from "./timelineReducer";
it("uses only actual context occupancy, marks 80 percent and clamps meter geometry", () => {
  const base = {inputTokens:80000,totalTokens:90000,outputTokens:10000} as UsageSnapshot;
  const view = render(<ContextRing usage={base} />); expect(screen.queryByRole("meter")).toBeNull();
  view.rerender(<ContextRing usage={{...base,contextUsed:80,contextWindow:100}} />);
  expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow","80"); expect(screen.getByRole("meter")).toHaveClass("is-warning");
  view.rerender(<ContextRing usage={{...base,contextUsed:120,contextWindow:100}} />); expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow","100");
  expect(screen.getByRole("meter")).toHaveAttribute("aria-valuetext",expect.stringContaining("120%"));
  view.rerender(<ContextRing usage={{...base,contextUsed:5,contextWindow:0}} />); expect(screen.queryByRole("meter")).toBeNull();
});
it("shows context-only snapshots without inventing input/output totals", () => {
  render(<UsagePill usage={{contextUsed:25,contextWindow:100} as UsageSnapshot} />);
  expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow","25");
});
