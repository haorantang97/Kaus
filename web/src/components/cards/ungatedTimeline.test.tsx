/* AD-126（批次十五第 1 件）：**能力门控只管"入口"，不管"已到达的事件"。**
 *
 * 真机现象：网关暂时离线 → 能力矩阵全 `unknown` → 旧的 `CardRenderer` 按 AD-71
 * 把工具行、推理行、计划行、终端行、审批卡全部静默隐藏，用户看到的是一条"中间
 * 缺了一截"的会话。这条测试就是那次事故的守卫：**全 `unknown` 的矩阵下，五类
 * 事件一条不少**。
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { CardRenderer } from "./CardRenderer";
import type { UiCapabilities } from "./capabilities";
import type { AnyTimelineItem, ToolItem } from "./timelineReducer";

const base = {
  order: 1,
  lastSequence: 1,
  terminal: true,
  runId: "run:1",
  parentRunId: null,
} as const;

/** 网关离线时后端给的就是这一份：每一题都是 `unknown`。 */
const ALL_UNKNOWN: UiCapabilities = {
  structuredEvents: "unknown",
  sessions: { list: "unknown", create: "unknown", resume: "unknown", history: "unknown", branch: "unknown" },
  card: {
    streaming: "unknown",
    tools: { calls: "unknown", output: "unknown" },
    terminal: "unknown",
    fileChanges: "unknown",
    artifacts: "unknown",
    plan: "unknown",
    reasoning: "unknown",
    permissions: "unknown",
    questions: "unknown",
    authentication: "unknown",
    usage: "unknown",
    interrupt: "unknown",
    attachments: "unknown",
  },
  externalCli: { supported: "unknown", resume: "unknown" },
  models: { mode: "fixed", reasoning: "unknown", providers: "unknown" },
  capabilityProjection: {},
};

const toolItem: ToolItem = {
  kind: "tool", itemId: "tool:1", name: "terminal", input: { preview: "pwd" }, output: "/repo", progress: null, status: "completed", ...base,
};

const items: AnyTimelineItem[] = [
  toolItem,
  { kind: "reasoning", itemId: "reasoning:1", status: "completed", summary: "想了想", ...base },
  { kind: "plan", itemId: "plan:1", entries: [{ content: "第一步", status: "completed" }, { content: "第二步", status: "pending" }], ...base },
  { kind: "terminal", itemId: "terminal:1", command: "ls -al", output: "a.txt", exitCode: 0, status: "completed", ...base },
  {
    kind: "interaction",
    itemId: "interaction:1",
    interactionKind: "permission",
    permission: {
      requestId: "p1",
      title: "允许运行 terminal？",
      detail: null,
      options: [{ optionId: "allow", label: "允许", kind: "accept" }],
    },
    question: null,
    authentication: null,
    status: "pending",
    decision: null,
    outcome: null,
    ...base,
  },
];

describe("AD-126：到达的事件不受能力门控", () => {
  it("全 unknown 的能力矩阵下，工具 / 推理 / 计划 / 终端 / 审批五类全部渲染", () => {
    render(
      <>
        {items.map((item) => (
          <CardRenderer key={item.itemId} item={item} caps={ALL_UNKNOWN} />
        ))}
      </>,
    );
    expect(screen.getByTestId("tool-row")).toBeInTheDocument();
    expect(screen.getByTestId("reasoning-row")).toBeInTheDocument();
    expect(screen.getByTestId("plan-row")).toBeInTheDocument();
    expect(screen.getByTestId("terminal-row")).toBeInTheDocument();
    expect(screen.getByText("允许运行 terminal？")).toBeInTheDocument();
  });

  it("工具输出栏只看有没有 output，不看 card.tools.output", async () => {
    const { rerender } = render(<CardRenderer item={toolItem} caps={ALL_UNKNOWN} />);
    // 展开：能力是 unknown，但 output 在，所以输出栏在。
    (await screen.findByTestId("tool-row")).querySelector("button")?.click();
    expect(await screen.findByText("/repo")).toBeInTheDocument();

    // 没有 output 的那条：输出栏整块缺席（不留空栏）。
    rerender(<CardRenderer item={{ ...toolItem, itemId: "tool:2", output: null }} caps={ALL_UNKNOWN} />);
    expect(screen.queryByText("/repo")).toBeNull();
  });
});
