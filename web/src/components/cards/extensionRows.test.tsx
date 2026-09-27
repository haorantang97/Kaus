/* 批次二十八第 4 件（AD-71）：ACP 的通知型扩展事件在页面上**一行都不留**。
 *
 * 真机现象：ACP 会话里 `available_commands_update` / `session_info_update` 每来
 * 一条，时间线上就多一行「Extension event」——没有标题、没有内容，只是把对话
 * 挤散。这里验两层：CardRenderer 对它返回 null；页面那一层连包裹它的壳都不生成
 * （只让 renderer 返回 null 的话，DOM 里会留下一串空 <div>）。
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { CardRenderer } from "./CardRenderer";
import { groupRows } from "../../pages/ConversationPage";
import { isHiddenTimelineItem } from "../../lib/timeline/extensionCards";
import {
  MODEL_ADOPTED_NAME,
  MODEL_ADOPTED_NAMESPACE,
  USER_MESSAGE_NAMESPACE,
  USER_MESSAGE_NAME,
} from "../../lib/timeline/reducer";
import type { AnyTimelineItem, TimelineItem } from "./timelineReducer";

const base = {
  order: 1,
  lastSequence: 1,
  terminal: true,
  runId: "run:1",
  parentRunId: null,
} as const;

function extensionItem(namespace: string, name: string, order = 1): AnyTimelineItem {
  return {
    kind: "extension",
    itemId: `extension:${namespace}:${name}`,
    ...base,
    order,
    namespace,
    name,
    data: { commands: ["/help"] },
  };
}

const userMessage: AnyTimelineItem = {
  kind: "message",
  itemId: "message:1",
  ...base,
  order: 2,
  role: "user",
  deltaChunks: {},
  finalText: "帮我看看这段代码",
  reasoningChunks: {},
};

describe("扩展事件不再排空行", () => {
  it("没有卡的扩展事件：CardRenderer 什么都不渲染", () => {
    const { container } = render(<CardRenderer item={extensionItem("acp", "session_info_update")} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText(/Extension event|扩展事件/)).toBeNull();
  });

  it("页面那一层把它整条滤掉：不生成包裹它的壳", () => {
    const items: TimelineItem[] = [
      extensionItem("acp", "available_commands_update", 1),
      userMessage,
      extensionItem("acp", "session_info_update", 3),
    ];
    const rows = groupRows(items.filter((item) => !isHiddenTimelineItem(item)));
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ kind: "item", item: { itemId: "message:1" } });
  });

  it("白名单里的那一条仍然渲染（规则是「没有卡才不显示」，不是「扩展事件都不显示」）", () => {
    const { container } = render(
      <CardRenderer item={extensionItem(USER_MESSAGE_NAMESPACE, USER_MESSAGE_NAME)} />,
    );
    expect(container).not.toBeEmptyDOMElement();
  });
});

/* batch31 / AD-155：模型采纳的那一行。 */
function adoptedItem(data: unknown): AnyTimelineItem {
  return {
    kind: "extension",
    itemId: "extension:kaus:model.adopted",
    ...base,
    namespace: MODEL_ADOPTED_NAMESPACE,
    name: MODEL_ADOPTED_NAME,
    data,
  };
}

describe("batch31：模型采纳是一行中性说明", () => {
  it("画出「引擎当前模型是 B，这条会话从这里起按 B 继续（原快照 A）」", () => {
    render(
      <CardRenderer
        item={adoptedItem({
          from: "gpt-5.6-sol",
          to: "gpt-5.6-terra",
          reason: "engine_not_conversation_scoped",
        })}
      />,
    );
    expect(
      screen.getByText(
        "引擎当前模型是 gpt-5.6-terra，这条会话从这里起按 gpt-5.6-terra 继续（原快照 gpt-5.6-sol）",
      ),
    ).toBeInTheDocument();
    // 中性一行，不是错误卡：不带「扩展事件」这种类型标题，也不可展开。
    expect(screen.queryByText(/Extension event|扩展事件/)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("没有原快照时不编一个出来", () => {
    render(<CardRenderer item={adoptedItem({ to: "gpt-5.6-terra" })} />);
    expect(
      screen.getByText("引擎当前模型是 gpt-5.6-terra，这条会话从这里起按 gpt-5.6-terra 继续"),
    ).toBeInTheDocument();
  });

  it("载荷说不出换成了什么 → 整条不渲染（N §13.1：没有来源就不编）", () => {
    const { container } = render(<CardRenderer item={adoptedItem({ from: "gpt-5.6-sol" })} />);
    expect(container).toBeEmptyDOMElement();
  });
});
