/* 批次二十八第 4 件（AD-71）：没有卡的扩展事件不渲染。
 *
 * 真机现象：ACP 会话的时间线上排着一串「Extension event」空行，来源是引擎每次
 * 都发的 `available_commands_update` / `session_info_update`——它们对用户没有
 * 任何含义。这里验判定本身；「页面上真的不出现那一行」在
 * `components/cards/extensionRows.test.tsx` 里验。
 */

import { describe, expect, it } from "vitest";

import { hasExtensionCard, isHiddenTimelineItem } from "./extensionCards";
import {
  MODEL_ADOPTED_NAME,
  MODEL_ADOPTED_NAMESPACE,
  USER_MESSAGE_NAME,
  USER_MESSAGE_NAMESPACE,
} from "./reducer";
import type { AnyTimelineItem } from "./reducer";

const base = {
  order: 1,
  lastSequence: 1,
  terminal: true,
  runId: "run:1",
  parentRunId: null,
} as const;

function extensionItem(namespace: string, name: string): AnyTimelineItem {
  return { kind: "extension", itemId: `extension:${name}`, ...base, namespace, name, data: null };
}

describe("扩展事件的白名单", () => {
  it("ACP 那两条通知型事件没有卡 → 整条不渲染", () => {
    for (const name of ["available_commands_update", "session_info_update"]) {
      expect(hasExtensionCard("acp", name)).toBe(false);
      expect(isHiddenTimelineItem(extensionItem("acp", name))).toBe(true);
    }
  });

  it("白名单是白名单：没登记过的一律不显示，而不是默认渲染成一行空标题", () => {
    expect(hasExtensionCard("acme", "whatever")).toBe(false);
    expect(hasExtensionCard(USER_MESSAGE_NAMESPACE, "something.else")).toBe(false);
  });

  it("用户自己那句话在白名单里（正常路径根本走不到扩展分支，这是保险）", () => {
    expect(hasExtensionCard(USER_MESSAGE_NAMESPACE, USER_MESSAGE_NAME)).toBe(true);
    expect(isHiddenTimelineItem(extensionItem(USER_MESSAGE_NAMESPACE, USER_MESSAGE_NAME))).toBe(false);
  });

  /* batch31 / AD-155 */
  it("模型采纳在白名单里：整条会话的模型在这一刻变了，不能静默", () => {
    expect(hasExtensionCard(MODEL_ADOPTED_NAMESPACE, MODEL_ADOPTED_NAME)).toBe(true);
    expect(
      isHiddenTimelineItem(extensionItem(MODEL_ADOPTED_NAMESPACE, MODEL_ADOPTED_NAME)),
    ).toBe(false);
  });

  it("别的种类的条目一概不受影响（这条规则只管扩展事件）", () => {
    const tool: AnyTimelineItem = {
      kind: "tool",
      itemId: "tool:1",
      ...base,
      name: "bash",
      status: "completed",
      input: null,
      output: null,
      progress: null,
    };
    expect(isHiddenTimelineItem(tool)).toBe(false);
  });
});
