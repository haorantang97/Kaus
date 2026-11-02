/* 主 Agent 代为审批 / 改设置（`kaus/proxy.action`）在时间线上留一行系统提示。 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { CardRenderer } from "./CardRenderer";
import { isHiddenTimelineItem } from "../../lib/timeline/extensionCards";
import type { AnyTimelineItem } from "./timelineReducer";

function note(data: unknown): AnyTimelineItem {
  return {
    kind: "extension", itemId: "extension:proxy:1", order: 1, lastSequence: 1, terminal: true,
    runId: null, parentRunId: null, namespace: "kaus", name: "proxy.action", data,
  };
}

describe("proxy action note", () => {
  it("shows an approval, with the chosen option as its tooltip", () => {
    const item = note({ proxy: "main_agent", action: "approved", subject: "允许写入 a.txt？", choice: "允许" });
    expect(isHiddenTimelineItem(item)).toBe(false);
    render(<CardRenderer item={item} />);
    expect(screen.getByText("主 Agent 已代为批准：允许写入 a.txt？")).toHaveAttribute("title", "允许");
  });

  it("names a denial and a permission change", () => {
    render(<>
      <CardRenderer item={note({ proxy: "main_agent", action: "denied", subject: "运行 rm" })} />
      <CardRenderer item={{ ...note({ proxy: "main_agent", action: "setting", setting: "approval", value: "bypass" }), itemId: "extension:proxy:2" }} />
    </>);
    expect(screen.getByText("主 Agent 已拒绝：运行 rm")).toBeInTheDocument();
    expect(screen.getByText("主 Agent 已将权限改为：完全访问")).toBeInTheDocument();
  });

  it("falls back to the server text for an unknown shape", () => {
    render(<CardRenderer item={note({ proxy: "main_agent", action: "later", text: "主 Agent 已处理" })} />);
    expect(screen.getByText("主 Agent 已处理")).toBeInTheDocument();
  });
});
