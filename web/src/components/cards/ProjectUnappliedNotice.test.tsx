/* D-03: Project capabilities that did not reach the session are visible on the timeline. */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { CardRenderer } from "./CardRenderer";
import { hasExtensionCard, PROJECT_UNAPPLIED_NAME, PROJECT_UNAPPLIED_NAMESPACE } from "../../lib/timeline/extensionCards";
import type { AnyTimelineItem } from "./timelineReducer";

function notice(data: unknown): AnyTimelineItem {
  return {
    kind: "extension", itemId: "extension:unapplied", order: 1, lastSequence: 1, terminal: true,
    runId: null, parentRunId: null, namespace: PROJECT_UNAPPLIED_NAMESPACE, name: PROJECT_UNAPPLIED_NAME, data,
  };
}

describe("Project capabilities not applied", () => {
  it("is whitelisted", () => {
    expect(hasExtensionCard(PROJECT_UNAPPLIED_NAMESPACE, PROJECT_UNAPPLIED_NAME)).toBe(true);
  });

  it("names the kinds, keeps ids in the tooltip and links to the Project", () => {
    render(<CardRenderer item={notice({
      projectId: "project:media",
      items: [
        { kind: "instructions", names: ["house-style"], reason: "not_materialized" },
        { kind: "mcp", names: ["files"], reason: "projection_failed" },
      ],
    })} />);
    const row = screen.getByTestId("project-unapplied");
    expect(row.textContent).toMatch(/Project (指令、MCP未生效|instructions, MCP not applied)/);
    const tooltip = row.querySelector("[title*='house-style']")?.getAttribute("title") ?? "";
    expect(tooltip).toMatch(/未应用或已过期|Not applied or out of date/);
    expect(tooltip).toMatch(/启动时投射失败|Projection failed at start/);
    expect(tooltip).not.toMatch(/not_materialized|projection_failed/);
    expect(row.querySelector("a")?.getAttribute("href")).toBe("/projects/project%3Amedia");
  });

  it("localizes known reason codes and keeps unknown ones verbatim", () => {
    render(<CardRenderer item={notice({
      projectId: "project:media",
      items: [
        { kind: "instructions", names: ["a"], reason: "no_convention" },
        { kind: "skills", names: ["b"], reason: "engine_specific_code" },
      ],
    })} />);
    const tooltip = screen.getByTestId("project-unapplied").querySelector("[title]")?.getAttribute("title") ?? "";
    expect(tooltip).toMatch(/未指定项目指令文件|instruction/i);
    expect(tooltip).not.toMatch(/no_convention/);
    expect(tooltip).toContain("engine_specific_code");
  });

  it("renders nothing for an empty payload", () => {
    const { container } = render(<CardRenderer item={notice({ items: [] })} />);
    expect(container).toBeEmptyDOMElement();
  });
});
