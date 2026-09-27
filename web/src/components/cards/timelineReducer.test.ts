/* timelineReducer 的快照与规则测试。
 *
 * 事件序列不是手写的，是 `web/scripts/export-fixtures.py` 从内核的 Mock 剧本
 * （kernel/drivers/mock/fixtures.py）导出的 JSON——前端 reducer 与内核 reducer
 * 吃同一串事件，这是"两份实现没漂移"的唯一可自动化证据。
 *
 * 重新导出：cd web && python3 scripts/export-fixtures.py
 */

import { describe, expect, it } from "vitest";

import authentication from "./__fixtures__/events.authentication-roundtrip.json";
import delegated from "./__fixtures__/events.delegated-run.json";
import extension from "./__fixtures__/events.extension-event.json";
import failure from "./__fixtures__/events.failure.json";
import fullLifecycle from "./__fixtures__/events.full-lifecycle.json";
import permission from "./__fixtures__/events.permission-roundtrip.json";
import question from "./__fixtures__/events.question-roundtrip.json";
import streamingReasoning from "./__fixtures__/events.streaming-reasoning.json";
import streamingToolOutput from "./__fixtures__/events.streaming-tool-output.json";
import textStream from "./__fixtures__/events.text-stream.json";
import toolLifecycle from "./__fixtures__/events.tool-lifecycle.json";

import {
  initialTimelineState,
  messageReasoningText,
  messageText,
  pendingInteractions,
  reduceEvent,
  reduceEvents,
  type AgentEventEnvelope,
  type InteractionItem,
  type MessageItem,
  type TimelineState,
  type ToolItem,
  USER_MESSAGE_NAME,
  USER_MESSAGE_NAMESPACE,
} from "./timelineReducer";

const FIXTURES: Record<string, unknown[]> = {
  "text-stream": textStream,
  "tool-lifecycle": toolLifecycle,
  "streaming-tool-output": streamingToolOutput,
  "streaming-reasoning": streamingReasoning,
  "permission-roundtrip": permission,
  "question-roundtrip": question,
  "authentication-roundtrip": authentication,
  "delegated-run": delegated,
  failure,
  "extension-event": extension,
  "full-lifecycle": fullLifecycle,
};

const events = (name: keyof typeof FIXTURES) => FIXTURES[name] as unknown as AgentEventEnvelope[];

function run(name: keyof typeof FIXTURES): TimelineState {
  return reduceEvents(initialTimelineState("conversation:fixture"), events(name));
}

/** 快照投影：只留下会影响渲染的东西，去掉 Set 之类不可序列化的字段。 */
function summarize(state: TimelineState) {
  return {
    runState: state.runState,
    activeRunId: state.activeRunId,
    lastSequence: state.lastSequence,
    dropped: { duplicates: state.droppedDuplicates, stale: state.droppedStale },
    error: state.error ? state.error.code : null,
    usage: state.usage,
    childRuns: state.childRuns.map((child) => ({
      runId: child.runId,
      parentRunId: child.parentRunId,
      state: child.state,
    })),
    items: state.items.map((item) => {
      const base = {
        kind: item.kind,
        itemId: item.itemId,
        terminal: item.terminal,
        runId: item.runId,
        parentRunId: item.parentRunId,
      };
      switch (item.kind) {
        case "message":
          return { ...base, role: item.role, text: messageText(item), reasoning: messageReasoningText(item) };
        case "tool":
          return { ...base, name: item.name, status: item.status, output: item.output, progress: item.progress };
        case "terminal":
          return { ...base, command: item.command, output: item.output, exitCode: item.exitCode };
        case "interaction":
          return {
            ...base,
            interactionKind: item.interactionKind,
            status: item.status,
            decision: item.decision,
            outcome: item.outcome,
          };
        case "reasoning":
          return { ...base, status: item.status, summary: item.summary };
        case "plan":
          return { ...base, entries: item.entries.map((entry) => entry.content) };
        case "file":
          return { ...base, path: item.path, operation: item.operation };
        case "artifact":
          return { ...base, artifactId: item.artifact.artifactId };
        case "diagnostic":
          return { ...base, level: item.level, message: item.message };
        case "lifecycle":
          return { ...base, eventType: item.eventType, detail: item.detail };
        case "extension":
          return { ...base, namespace: item.namespace, name: item.name };
      }
    }),
  };
}

describe("快照：内核剧本逐条归并后的时间线", () => {
  for (const name of Object.keys(FIXTURES)) {
    it(name, () => {
      expect(summarize(run(name))).toMatchSnapshot();
    });
  }
});

/* 用户消息（批次八第 6 件）：内核那三条 reducer 断言的镜像，见
   kernel/runtime/tests/test_user_message_timeline.py。事件本身是 Session Host 发的，
   Mock 剧本里没有，所以这里手搓信封而不是从 fixture 拿。 */
function extensionEnvelope(namespace: string, name: string, data: unknown, sequence = 0): AgentEventEnvelope {
  return {
    ...events("text-stream")[0],
    eventId: `evt-user-${sequence}`,
    sequence,
    runId: null,
    parentRunId: null,
    event: { type: "extension.event", namespace, name, data },
  } as AgentEventEnvelope;
}

describe("批次八：用户消息走 extension.event 进时间线", () => {
  it("kaus / user.message 归成一条 role=user 的消息卡，不是通用事件卡", () => {
    const state = reduceEvent(
      initialTimelineState("conversation:x"),
      extensionEnvelope(USER_MESSAGE_NAMESPACE, USER_MESSAGE_NAME, { text: "帮我看看这段代码", role: "user" }),
    );
    const messages = state.items.filter((item) => item.kind === "message") as MessageItem[];
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe("user");
    expect(messageText(messages[0])).toBe("帮我看看这段代码");
    // 用户消息没有流式增量，一落地就是终态。
    expect(messages[0].terminal).toBe(true);
    expect(state.items.filter((item) => item.kind === "extension")).toHaveLength(0);
  });

  it("只认这一对 namespace/name，别的扩展事件照旧走通用卡（N §8.2）", () => {
    let state = reduceEvent(initialTimelineState("c"), extensionEnvelope("acme", "user.message", null, 0));
    state = reduceEvent(state, extensionEnvelope(USER_MESSAGE_NAMESPACE, "something.else", null, 1));
    expect(state.items.filter((item) => item.kind === "extension")).toHaveLength(2);
    expect(state.items.filter((item) => item.kind === "message")).toHaveLength(0);
  });

  it("data 不是对象时按空文本处理，不崩", () => {
    const state = reduceEvent(
      initialTimelineState("c"),
      extensionEnvelope(USER_MESSAGE_NAMESPACE, USER_MESSAGE_NAME, "裸字符串"),
    );
    const messages = state.items.filter((item) => item.kind === "message") as MessageItem[];
    expect(messages.map((item) => messageText(item))).toEqual([""]);
  });
});

describe("N §7.3 规则 2：tool.updated 更新同一张卡", () => {
  it("三次 update + 一次 complete 只有一张工具卡", () => {
    const state = run("tool-lifecycle");
    const tools = state.items.filter((item) => item.kind === "tool");
    expect(tools).toHaveLength(1);
    const tool = tools[0] as ToolItem;
    expect(tool.status).toBe("completed");
    expect(tool.terminal).toBe(true);
  });

  it("AD-27：增量分片追加，随后的 cumulative 全量整体替换（不重复拼接）", () => {
    const tool = run("streaming-tool-output").items.find((item) => item.kind === "tool") as ToolItem;
    expect(tool.output).toBe("line-1\nline-2\nline-3\n");
  });
});

describe("N §7.3 规则 3 / AD-27：文本与思考各归各的消息", () => {
  it("delta 按 messageId 合并成一段正文", () => {
    const message = run("text-stream").items.find((item) => item.kind === "message") as MessageItem;
    expect(messageText(message)).toBe("Hello, world!");
  });

  it("reasoning.delta 进思考区，不污染正文", () => {
    const state = run("streaming-reasoning");
    const message = state.items.find((item) => item.kind === "message") as MessageItem;
    expect(messageText(message)).toBe("结论：可以。");
    expect(messageReasoningText(message)).toBe("先看输入，再想一步，得出结论。");
    // reasoning.status 是 run 粒度的独立条目，与消息的思考区互不覆盖。
    expect(state.items.filter((item) => item.kind === "reasoning")).toHaveLength(1);
  });
});

describe("N §7.3 规则 4：交互请求的闭环", () => {
  it.each([
    ["permission-roundtrip", "permission"],
    ["question-roundtrip", "question"],
    ["authentication-roundtrip", "authentication"],
  ] as const)("%s 收到 resolved 后关卡", (fixture: keyof typeof FIXTURES, kind: string) => {
    const state = run(fixture);
    const items = state.items.filter((item) => item.kind === "interaction") as InteractionItem[];
    expect(items).toHaveLength(1);
    expect(items[0].interactionKind).toBe(kind);
    expect(items[0].status).toBe("resolved");
    expect(pendingInteractions(state)).toHaveLength(0);
  });

  it("答之前是 pending（Composer 据此变形）", () => {
    const half = events("permission-roundtrip").slice(0, 3);
    const state = reduceEvents(initialTimelineState("c"), half);
    expect(pendingInteractions(state)).toHaveLength(1);
  });
});

describe("N §7.3 规则 9：去重 / 乱序 / 终态收敛", () => {
  it("整串重放一遍不产生重复卡片", () => {
    const once = run("full-lifecycle");
    const twice = reduceEvents(once, events("full-lifecycle"));
    expect(twice.items).toHaveLength(once.items.length);
    expect(twice.droppedDuplicates).toBe(events("full-lifecycle").length);
  });

  it("message.completed 之后迟到的 delta 被丢弃", () => {
    const base = run("text-stream");
    const late = {
      ...events("text-stream")[3],
      eventId: "evt-late",
      sequence: 99,
      event: { type: "message.delta", messageId: "msg-text", text: "（迟到）" },
    } as AgentEventEnvelope;
    const after = reduceEvent(base, late);
    const message = after.items.find((item) => item.kind === "message") as MessageItem;
    expect(messageText(message)).toBe("Hello, world!");
    expect(after.droppedStale).toBe(base.droppedStale + 1);
  });

  it("run 终态后迟到的 run.started 不把状态拉回 running", () => {
    const base = run("text-stream");
    const late = {
      ...events("text-stream")[0],
      eventId: "evt-late-start",
      sequence: 99,
    } as AgentEventEnvelope;
    expect(reduceEvent(base, late).runState).toBe("completed");
  });

  it("run.failed 收敛到 failed 并留下结构化错误", () => {
    const state = run("failure");
    expect(state.runState).toBe("failed");
    expect(state.error?.code).toBe("mock.boom");
  });
});

describe("AD-08：子 run 不改变父 run 的状态", () => {
  it("子 run 完成时父 run 仍在运行；父 run 完成后才收敛", () => {
    const all = events("delegated-run");
    const beforeParentEnd = reduceEvents(initialTimelineState("c"), all.slice(0, all.length - 2));
    expect(beforeParentEnd.runState).toBe("running");
    expect(beforeParentEnd.childRuns).toHaveLength(1);
    expect(beforeParentEnd.childRuns[0].state).toBe("completed");

    const final = reduceEvents(initialTimelineState("c"), all);
    expect(final.runState).toBe("completed");
    expect(final.activeRunId).toBe("run-parent");
    // 子 run 的卡片照样看得见，并且记得自己属于谁。
    const childCards = final.items.filter((item) => item.parentRunId === "run-parent");
    expect(childCards.length).toBeGreaterThan(0);
  });
});

describe("N §8.2：不认识的事件也要看得见", () => {
  it("extension.event 归成 extension 条目", () => {
    const item = run("extension-event").items.find((entry) => entry.kind === "extension");
    expect(item).toBeDefined();
  });

  it("信封里出现前端不认识的 type 时不崩，落成 extension 条目", () => {
    const envelope = {
      ...events("text-stream")[0],
      eventId: "evt-unknown",
      sequence: 500,
      event: { type: "future.thing", whatever: 1 },
    } as unknown as AgentEventEnvelope;
    const state = reduceEvent(initialTimelineState("c"), envelope);
    expect(state.items).toHaveLength(1);
    expect(state.items[0].kind).toBe("extension");
  });
});

describe("用量", () => {
  it("usage.updated 落到 state.usage（缺的字段保持 null，不伪造）", () => {
    const state = run("full-lifecycle");
    expect(state.usage).toMatchObject({ inputTokens: 120, outputTokens: 40, totalTokens: 160 });
    expect(state.usage?.costUsd).toBeNull();
  });
});

/* 以下三条从 `lib/timeline/reducer.test.ts`（会话页的简版 reducer）合并而来：
   简版被全量镜像取代，它验的三件事在这里用同一串 mock 剧本重验一遍。 */
describe("会话页简版 reducer 合并进来的用例", () => {
  it("message.delta 追加到同一个气泡，不产生第二张卡", () => {
    const state = run("text-stream");
    const messages = state.items.filter((item) => item.kind === "message");
    expect(messages).toHaveLength(1);
    expect(messageText(messages[0] as MessageItem)).toBe("Hello, world!");
    expect(messages[0].terminal).toBe(true);
  });

  it("run.* 驱动 runState；未知事件看得见（边界②：不许空白）", () => {
    const all = events("text-stream");
    let state = initialTimelineState("c");
    state = reduceEvent(state, all[0]);
    expect(all[0].event.type).toBe("run.started");
    expect(state.runState).toBe("running");
    state = reduceEvent(state, {
      ...all[0],
      eventId: "evt-odd",
      sequence: 900,
      event: { type: "tool.mystery", callId: "t1" },
    } as unknown as AgentEventEnvelope);
    expect(state.items.some((item) => item.kind === "extension")).toBe(true);
    state = reduceEvents(state, all.slice(1));
    expect(state.runState).toBe("completed");
  });

  it("按 eventId 去重；lastSequence 只向前推进", () => {
    const all = events("text-stream");
    let state = reduceEvents(initialTimelineState("c"), all);
    const count = state.items.length;
    const highest = state.lastSequence;
    state = reduceEvent(state, all[0]);
    expect(state.items).toHaveLength(count);
    expect(state.droppedDuplicates).toBe(1);
    expect(state.lastSequence).toBe(highest);
  });
});
