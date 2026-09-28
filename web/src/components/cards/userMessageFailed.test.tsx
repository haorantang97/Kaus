/* batch38 第 2 件 / 批次三十七 R6：那句没被接下的话在界面上长什么样。
 *
 * reducer 那一半在 `lib/timeline/userMessageFailed.test.ts` 里。这里验界面：
 * 气泡置灰、接一行原因、给一枚「重发」（重发 = 用同一段正文再发一次），
 * 以及配不上编号时那条中性系统提示确实出现——被藏起来的失败等于骗人说它发出去了。
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CardRenderer } from "./CardRenderer";
import type { MessageActions } from "./TextCard";
import {
  USER_MESSAGE_FAILED_NAME,
  USER_MESSAGE_NAMESPACE,
  type AnyTimelineItem,
  type MessageItem,
} from "./timelineReducer";

const base = {
  order: 1,
  lastSequence: 1,
  terminal: true,
  runId: null,
  parentRunId: null,
} as const;

function userMessage(overrides: Partial<MessageItem> = {}): MessageItem {
  return {
    kind: "message",
    itemId: "message:m1",
    ...base,
    role: "user",
    deltaChunks: {},
    finalText: "帮我跑一下测试",
    reasoningChunks: {},
    deliveryStatus: "ok",
    clientRef: "ref-1",
    ...overrides,
  };
}

function renderItem(
  item: AnyTimelineItem,
  messageActions?: (item: AnyTimelineItem) => MessageActions | null,
) {
  return render(<CardRenderer item={item} callbacks={{ messageActions }} />);
}

describe("投递失败的用户消息（R6）", () => {
  it("正常那条不置灰、也没有原因行", () => {
    const { container } = renderItem(userMessage());
    expect(container.querySelector(".kaus-msg.is-undelivered")).toBeNull();
    expect(screen.queryByTestId("message-failed-note")).toBeNull();
  });

  it("历史拒绝事件按错误码显示短状态，保留原始诊断和图标重发", async () => {
    const onRetryFailed = vi.fn();
    const { container } = renderItem(
      userMessage({
        deliveryStatus: "failed",
        failureCode: "turn_already_running",
        failureMessage: "上一轮还在运行，等它结束再发，或先点停止",
      }),
      () => ({ onRetryFailed }),
    );

    const bubble = container.querySelector(".kaus-msg.is-user");
    expect(bubble).toHaveClass("is-failed");
    expect(bubble).toHaveClass("is-undelivered");
    expect(bubble).toHaveAttribute("data-delivery", "failed");
    // 正文一个字不少：那句话确实被说出来过。
    expect(screen.getByText("帮我跑一下测试")).toBeInTheDocument();
    expect(screen.getByTestId("message-failed-note")).toHaveTextContent(
      "未发送 · 上一轮未结束",
    );
    expect(screen.getByTestId("message-failed-note")).not.toHaveTextContent("上一轮还在运行");
    expect(screen.getByTitle("上一轮还在运行，等它结束再发，或先点停止")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重发" })).toHaveTextContent(/^$/);

    await userEvent.click(screen.getByRole("button", { name: "重发" }));
    // 重发拿到的是同一段正文；新编号由页面那一层生成（走的还是首发那条路）。
    expect(onRetryFailed).toHaveBeenCalledWith("帮我跑一下测试");
  });

  it("缺少诊断文本时根据已知错误码显示原因；无回调时不提供重发", () => {
    renderItem(userMessage({ deliveryStatus: "failed", failureCode: "unsupported_capability" }), () => ({}));
    expect(screen.getByTestId("message-failed-note")).toHaveTextContent("未发送 · 引擎不支持此操作");
    expect(screen.queryByRole("button", { name: "重发" })).toBeNull();
  });

  it("模糊网络失败保留未确认语义，禁止重发并提供只读核对入口", async () => {
    const onRetryFailed = vi.fn();
    const onCheckDelivery = vi.fn();
    const { container } = renderItem(userMessage({ deliveryStatus: "failed", failureCode: "send_failed", failureMessage: "network timeout" }), () => ({ onRetryFailed, onCheckDelivery, lockedWhileRunning: true }));
    expect(screen.getByTestId("message-failed-note")).toHaveTextContent("发送结果未确认");
    expect(screen.getByTestId("message-failed-note")).toHaveTextContent(/^发送结果未确认$/);
    expect(screen.getByTitle("network timeout")).toBeInTheDocument();
    expect(container.querySelector(".kaus-msg.is-user")).toHaveAttribute("data-delivery", "unconfirmed");
    expect(screen.queryByRole("button", { name: "重发" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "查看会话状态" }));
    expect(onCheckDelivery).toHaveBeenCalledTimes(1);
    expect(onRetryFailed).not.toHaveBeenCalled();
  });

  it("未知或缺失的失败码不解释为确定未发送", () => {
    renderItem(userMessage({ deliveryStatus: "failed" }), () => ({ onRetryFailed: vi.fn() }));
    expect(screen.getByTestId("message-failed-note")).toHaveTextContent("发送结果未确认");
    expect(screen.queryByRole("button", { name: "重发" })).toBeNull();
  });

  it("未配对的模糊失败事件也保留结果未确认提示", () => {
    renderItem({ kind: "extension", itemId: "extension:unknown", ...base, namespace: USER_MESSAGE_NAMESPACE, name: USER_MESSAGE_FAILED_NAME, data: { code: "message_rejected", message: "Gateway response lost" } });
    expect(screen.getByText(/发送结果未确认/)).toBeInTheDocument();
    expect(screen.queryByText(/有一句话没有被处理/)).toBeNull();
  });

  it("配不上编号的那一档：留一行中性系统提示，不静默丢掉", () => {
    renderItem({
      kind: "extension",
      itemId: "extension:e1",
      ...base,
      namespace: USER_MESSAGE_NAMESPACE,
      name: USER_MESSAGE_FAILED_NAME,
      data: { code: "runtime_not_active", message: "引擎没在跑" },
    });
    expect(screen.getByText("消息未发送 · 引擎未运行")).toBeInTheDocument();
    expect(screen.getByTitle("引擎没在跑")).toBeInTheDocument();
  });
});
