/* Receipt controls retain delivery semantics and make recovery accessible. */

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
  it("正常消息没有失败状态控件", () => {
    const { container } = renderItem(userMessage());
    expect(container.querySelector(".kaus-msg.is-undelivered")).toBeNull();
    expect(screen.queryByRole("button", { name: /未发送|发送结果未确认/ })).toBeNull();
  });

  it("历史拒绝事件显示图标，按需查看原始诊断并可重发", async () => {
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
    const status = screen.getByRole("button", { name: "未发送 · 上一轮未结束" });
    expect(status).toHaveTextContent(/^$/);
    expect(status).toHaveAttribute("aria-expanded", "false");
    expect(status.closest(".kaus-msg-footer")).not.toBeNull();
    expect(screen.queryByText("上一轮还在运行，等它结束再发，或先点停止")).toBeNull();
    await userEvent.click(status);
    expect(status).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("note")).toHaveTextContent("上一轮还在运行，等它结束再发，或先点停止");
    expect(onRetryFailed).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "重发" })).toHaveTextContent(/^$/);

    await userEvent.click(screen.getByRole("button", { name: "重发" }));
    // 重发拿到的是同一段正文；新编号由页面那一层生成（走的还是首发那条路）。
    expect(onRetryFailed).toHaveBeenCalledWith("帮我跑一下测试");
  });

  it("缺少诊断文本时根据已知错误码显示原因；无回调时不提供重发", () => {
    renderItem(userMessage({ deliveryStatus: "failed", failureCode: "unsupported_capability" }), () => ({}));
    expect(screen.getByRole("button", { name: "未发送 · 引擎不支持此操作" })).toHaveTextContent(/^$/);
    expect(screen.queryByRole("button", { name: "重发" })).toBeNull();
  });

  it("模糊网络失败保留未确认语义，禁止重发并提供只读核对入口", async () => {
    const onRetryFailed = vi.fn();
    const onCheckDelivery = vi.fn();
    const { container } = renderItem(userMessage({ deliveryStatus: "failed", failureCode: "send_failed", failureMessage: "network timeout" }), () => ({ onRetryFailed, onCheckDelivery, lockedWhileRunning: true }));
    const status = screen.getByRole("button", { name: "发送结果未确认" });
    expect(status).toHaveTextContent(/^$/);
    await userEvent.click(status);
    expect(screen.getByRole("note")).toHaveTextContent("network timeout");
    expect(container.querySelector(".kaus-msg.is-user")).toHaveAttribute("data-delivery", "unconfirmed");
    expect(screen.queryByRole("button", { name: "重发" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "查看会话状态" }));
    expect(onCheckDelivery).toHaveBeenCalledTimes(1);
    expect(onRetryFailed).not.toHaveBeenCalled();
  });

  it("未知或缺失的失败码不解释为确定未发送", () => {
    renderItem(userMessage({ deliveryStatus: "failed" }), () => ({ onRetryFailed: vi.fn() }));
    expect(screen.getByRole("button", { name: "发送结果未确认" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重发" })).toBeNull();
  });

  it("键盘可查看及关闭诊断，禁用的重发不会触发提交", async () => {
    const retry = vi.fn();
    renderItem(userMessage({ deliveryStatus: "failed", failureCode: "turn_already_running", failureMessage: "driver busy" }),
      () => ({ onRetryFailed: retry, lockedWhileRunning: true }));
    const user = userEvent.setup();
    await user.tab(); // Copy.
    await user.tab(); // Delivery status.
    const status = screen.getByRole("button", { name: "未发送 · 上一轮未结束" });
    expect(status).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("note")).toHaveTextContent("driver busy");
    await user.keyboard("{Escape}");
    expect(status).toHaveFocus();
    expect(status).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByRole("button", { name: "重发" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "重发" }));
    expect(retry).not.toHaveBeenCalled();
  });

  it("点击状态外部关闭诊断，不改变消息或发送状态", async () => {
    renderItem(userMessage({ deliveryStatus: "failed", failureCode: "unsupported_capability", failureMessage: "unsupported input" }));
    const user = userEvent.setup();
    const status = screen.getByRole("button", { name: "未发送 · 引擎不支持此操作" });
    await user.click(status);
    expect(screen.getByRole("note")).toBeInTheDocument();
    await user.click(screen.getByText("帮我跑一下测试"));
    expect(status).toHaveAttribute("aria-expanded", "false");
    expect(status.closest(".kaus-msg")).toHaveAttribute("data-delivery", "failed");
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
