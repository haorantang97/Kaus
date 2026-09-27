/* batch38 第 2 件 / 批次三十七 R6：`kaus/user.message.failed` 的渲染语义。
 *
 * 真机现象（评审 R6 的复现）：引擎在接受之前就拒了那一句，但事件库里躺着的仍是一条
 * 普普通通的 `kaus/user.message`——刷新之后它与被正常处理过的一模一样，重发还会显示
 * 两遍。AD-105 的口径不回滚已经落库的正文，补的是一条状态事件。
 *
 * 这组用例钉住 reducer 那一半（配对、置灰、不新建卡片、配不上时留一行）；
 * 「置灰的气泡长什么样」在 `components/cards/userMessageFailed.test.tsx` 里验。
 */

import { describe, expect, it } from "vitest";

import { hasExtensionCard } from "./extensionCards";
import {
  initialTimelineState,
  messageDeliveryStatus,
  messageText,
  reduceEvent,
  reduceEvents,
  USER_MESSAGE_FAILED_NAME,
  USER_MESSAGE_NAME,
  USER_MESSAGE_NAMESPACE,
  type AgentEventEnvelope,
  type MessageItem,
  type TimelineState,
} from "./reducer";

function envelope(name: string, data: unknown, sequence: number): AgentEventEnvelope {
  return {
    eventId: `evt-${sequence}`,
    conversationId: "conversation:x",
    sequence,
    runId: null,
    parentRunId: null,
    occurredAt: "2026-09-07T00:00:00Z",
    source: { backendId: "b", nativeSessionId: null },
    event: { type: "extension.event", namespace: USER_MESSAGE_NAMESPACE, name, data },
  } as unknown as AgentEventEnvelope;
}

const sent = (text: string, clientRef: string | null, sequence: number) =>
  envelope(USER_MESSAGE_NAME, clientRef ? { text, clientRef } : { text }, sequence);

const failed = (data: unknown, sequence: number) =>
  envelope(USER_MESSAGE_FAILED_NAME, data, sequence);

const userMessages = (state: TimelineState) =>
  state.items.filter((item) => item.kind === "message" && item.role === "user") as MessageItem[];

describe("kaus/user.message.failed（R6）", () => {
  it("登记在白名单里：它是产品自己的事件，不能被「未登记 → 不显示」静默吃掉", () => {
    expect(hasExtensionCard(USER_MESSAGE_NAMESPACE, USER_MESSAGE_FAILED_NAME)).toBe(true);
  });

  it("按 clientRef 配回那条用户消息：置灰 + 记下 code/message，且不新建卡片", () => {
    const state = reduceEvents(initialTimelineState("conversation:x"), [
      sent("第一句", "ref-1", 0),
      sent("第二句", "ref-2", 1),
      failed({ clientRef: "ref-1", code: "turn_already_running", message: "上一轮还在跑" }, 2),
    ]);

    const messages = userMessages(state);
    expect(messages.map(messageText)).toEqual(["第一句", "第二句"]);
    // 配对靠编号不靠位置：被标的是**第一**条，不是最后一条。
    expect(messageDeliveryStatus(messages[0])).toBe("failed");
    expect(messages[0].failureCode).toBe("turn_already_running");
    expect(messages[0].failureMessage).toBe("上一轮还在跑");
    expect(messageDeliveryStatus(messages[1])).toBe("ok");
    // 不新建卡片：时间线长度不变，「这句话说了几遍」照旧答得出来。
    expect(state.items).toHaveLength(2);
    expect(state.items.filter((item) => item.kind === "extension")).toHaveLength(0);
  });

  it("没给 clientRef 时退回「最后一条还没被标失败的用户消息」", () => {
    let state = reduceEvents(initialTimelineState("c"), [
      sent("第一句", null, 0),
      sent("第二句", null, 1),
      failed({ code: "message_rejected", message: "网关拒了" }, 2),
    ]);
    expect(userMessages(state).map(messageDeliveryStatus)).toEqual(["ok", "failed"]);

    // 再来一条：往前顺推，不会把同一条标两遍。
    state = reduceEvent(state, failed({ code: "message_rejected", message: "又拒了" }, 3));
    expect(userMessages(state).map(messageDeliveryStatus)).toEqual(["failed", "failed"]);
  });

  it("编号配不上时不碰任何一条消息，只留一条扩展条目（中性系统一行）", () => {
    const state = reduceEvents(initialTimelineState("c"), [
      sent("第一句", "ref-1", 0),
      failed({ clientRef: "ref-x", code: "runtime_not_active", message: "引擎没在跑" }, 1),
    ]);
    // 凭空标一条别的消息比不标更糟：一条都不许被改。
    expect(userMessages(state).map(messageDeliveryStatus)).toEqual(["ok"]);
    const extensions = state.items.filter((item) => item.kind === "extension");
    expect(extensions).toHaveLength(1);
    expect(extensions[0].kind === "extension" && extensions[0].name).toBe(USER_MESSAGE_FAILED_NAME);
  });

  it("重放幂等：同一串事件跑两遍结果一样，重复 eventId 被丢弃", () => {
    const stream = [
      sent("第一句", "ref-1", 0),
      failed({ clientRef: "ref-1", code: "turn_already_running", message: "上一轮还在跑" }, 1),
    ];
    const once = reduceEvents(initialTimelineState("c"), stream);
    const twice = reduceEvents(once, stream);
    expect(twice.items).toEqual(once.items);
    expect(twice.droppedDuplicates).toBe(2);
  });

  it("data 不是对象（裸字符串）也不许崩：没编号、没人话，按兜底那档走", () => {
    const state = reduceEvents(initialTimelineState("c"), [
      sent("第一句", "ref-1", 0),
      failed("裸字符串", 1),
    ]);
    const message = userMessages(state)[0];
    expect(messageDeliveryStatus(message)).toBe("failed");
    expect(message.failureCode).toBeNull();
    expect(message.failureMessage).toBeNull();
  });
});
