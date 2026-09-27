/* Assistant prose stays unboxed; user messages use a quiet bubble.
 * Copy and time are reachable below the body, including received streaming text.
 * Callbacks only govern actions that send or edit messages, never read-only copy. */

import { Component, type ReactNode, useState } from "react";

import { useLocale } from "../../i18n";
import { StreamCursor } from "./CardShell";
import { messageDeliveryStatus, messageStatus, messageText, type MessageItem } from "./timelineReducer";
import { Pencil, RotateCcw } from "lucide-react";
import { MarkdownContent } from "./MarkdownContent";
import { FileReference } from "./ArtifactPreview";
import { CopyButton } from "./CopyButton";
import { useContentText } from "./contentStrings";
import { isConfirmedMessageRejection } from "../../lib/messageDelivery";
import "./conversationContent.css";

export interface MessageActions {
  /** 助手消息上的「重发上一条」；没有可重发的上一条时调用方不传。 */
  onResendPrevious?: () => void;
  /** 用户消息上的「编辑重发」：把正文填回输入框并聚焦，**不自动发送**。 */
  onEditResend?: (text: string) => void;
  /* batch38 / R6：这一句引擎没接下（`deliveryStatus === "failed"`）时的「重发」。
     重发 = 用同一段正文走首发那条路再发一次（新的对账编号），不是"恢复"这一条：
     那条正文已经在会话里，AD-105 说了不回滚。调用方不传就只置灰不给入口。 */
  onRetryFailed?: (text: string) => void;
  /** Read-only reconciliation when a transport failure leaves delivery uncertain. */
  onCheckDelivery?: () => void;
  /* batch39 第 2 件：上一轮还在跑。这一刻「重发」照旧**画出来**（入口的位置不该跟着
     状态忽隐忽现），但禁用并挂上「等本轮结束」；同一刻其余动作一枚不给——它们是
     "再发一句"，运行中给了只会打架。本轮一结束调用方就换回 `onRetryFailed`。 */
  lockedWhileRunning?: boolean;
}

function MessageActionBar({
  text,
  copyLabel,
  extraLabel,
  onExtra,
  ariaLabel,
  copiedLabel,
  editing,
}: {
  text: string;
  copyLabel: string;
  extraLabel: string | null;
  onExtra: (() => void) | null;
  ariaLabel: string;
  copiedLabel: string;
  editing: boolean;
}) {
  return (
    <div className="kaus-msg-actions" data-testid="message-actions" aria-label={ariaLabel}>
      {text && <CopyButton className="kaus-msg-action" text={text} label={copyLabel} copiedLabel={copiedLabel} />}
      {onExtra && extraLabel && (
        <button type="button" className="kaus-msg-action" onClick={onExtra} aria-label={extraLabel} title={extraLabel}>
          {editing ? <Pencil size={15} aria-hidden="true" /> : <RotateCcw size={15} aria-hidden="true" />}
        </button>
      )}
    </div>
  );
}

/* batch37 第 2 件（外部评审 R8）：一条消息的渲染塌了，**只塌这一条**。
 *
 * R8 复现的是 `renderMarkdown` 抛异常（占位符碰撞）。那个根因已经在 `lib/md.ts` 修掉，
 * 但"某条正文能把整棵应用树带走"这件事本身才是要害：现有错误边界在 `main.tsx`，
 * 包的是整个仪表盘，于是一条消息的格式化失败会换来整屏恢复界面——正在跑的会话、
 * 侧栏、终端全部消失。这里给每条助手正文套一层小边界：它塌了就把**原文**照常摆出来，
 * 加一句中性说明（★H：不上红，这不是错误状态，是"这条没排版成功"）。全局那层留着，
 * 仍然是最后一道兜底。 */
function MarkdownFallback({ text }: { text: string }) {
  const { t } = useLocale();
  return (
    <>
      <div className="kaus-prose-empty">{t("card.text.renderFailed")}</div>
      <div style={{ whiteSpace: "pre-wrap" }}>{text}</div>
    </>
  );
}

export class MarkdownBoundary extends Component<{ text: string; children: ReactNode }, { failed: boolean }> {
  state: { failed: boolean } = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    console.error("[dashboard] markdown render failure", error);
  }

  componentDidUpdate(previous: { text: string }) {
    /* 正文变了（流式吐字、重发）就再试一次：上一段排不出来不代表下一段也排不出来。 */
    if (this.state.failed && previous.text !== this.props.text) this.setState({ failed: false });
  }

  render() {
    if (this.state.failed) return <MarkdownFallback text={this.props.text} />;
    return this.props.children;
  }
}

export function TextCard({
  item,
  time,
  actions = null,
  conversationId,
}: {
  item: MessageItem;
  time?: string;
  actions?: MessageActions | null;
  conversationId?: string;
}) {
  const { t } = useLocale();
  const c = useContentText();
  const streaming = messageStatus(item) === "streaming";
  const text = messageText(item);
  /* batch17 第 3 件：动作不能只在**鼠标悬停**下可达。消息块内任意一枚按钮拿到
     焦点（键盘 Tab）就把这排动作显出来；触控（无 hover）由 CSS 的
     `@media (hover: none)` 常显。悬停照旧走 CSS，不进 React。 */
  const [focused, setFocused] = useState(false);
  const focusProps = {
    onFocus: () => setFocused(true),
    onBlur: () => setFocused(false),
  };
  const visible = focused ? "true" : undefined;
  const extraAllowed = !streaming && !actions?.lockedWhileRunning;
  const onExtra = extraAllowed ? (item.role === "user" ? (actions?.onEditResend ? () => actions.onEditResend?.(text) : null) : actions?.onResendPrevious ?? null) : null;
  const footer = (
    <div className="kaus-msg-footer">
      {(text || onExtra) && <MessageActionBar
        text={text}
        copyLabel={t("conversation.action.copy")}
        copiedLabel={t("conversation.action.copied")}
        editing={item.role === "user"}
        ariaLabel={t("conversation.action.aria")}
        extraLabel={t(item.role === "user" ? "conversation.action.editResend" : "conversation.action.resendPrevious")}
        onExtra={onExtra}
      />}
      {time && <time className="kaus-msg-time" title={time}>{time}</time>}
    </div>
  );

  if (item.role === "user") {
    /* batch38 / R6：引擎在接受之前就拒了这一句。正文不撤（AD-105：说过的话不回滚），
       但气泡置灰、接一行原因、给一枚「重发」——沿用本地占位失败气泡那套 class，
       让"刷新前"和"刷新后"看起来是同一件事，而不是两种失败。 */
    const deliveryFailed = messageDeliveryStatus(item) === "failed";
    const undelivered = deliveryFailed && isConfirmedMessageRejection(item);
    const unconfirmed = deliveryFailed && !undelivered;
    const reason = item.failureMessage || "";
    return (
      <div
        className={`kaus-msg kaus-content-message is-user${undelivered ? " is-failed is-undelivered" : unconfirmed ? " is-delivery-unconfirmed" : ""}`}
        data-actions-visible={visible}
        data-delivery={undelivered ? "failed" : unconfirmed ? "unconfirmed" : undefined}
        {...focusProps}
      >
        {(text || item.attachments?.length) ? <div className="kaus-msg-body">
          {text}
          {!!item.attachments?.length && <div className="kaus-msg-attachments">{item.attachments.map((attachment, index) => <FileReference key={`${attachment.ref}:${index}`} source={{ uri: attachment.ref, title: attachment.name, mimeType: attachment.mimeType, sizeBytes: attachment.size }} conversationId={conversationId} />)}</div>}
        </div> : null}
        {deliveryFailed && (
          <div className="kaus-msg-note" data-testid="message-failed-note">
            {unconfirmed ? <><strong>{c("chat.content.deliveryUnconfirmed")}</strong><span className="kaus-delivery-explanation">{c("chat.content.deliveryCheckFirst")}</span>{reason && <span className="kaus-delivery-explanation">{reason}</span>}</> : reason
              ? t("conversation.message.failed", { reason })
              : t("conversation.message.failed.noReason")}
            {/* batch39 第 2 件：运行中也画这一枚，只是禁用 + 一句 title 说明什么时候能点。 */}
            {undelivered && (actions?.onRetryFailed || actions?.lockedWhileRunning) && (
              <button
                type="button"
                className="kaus-msg-retry"
                data-testid="message-retry"
                disabled={!!actions.lockedWhileRunning}
                title={actions.lockedWhileRunning ? t("conversation.message.resend.wait") : undefined}
                onClick={() => actions.onRetryFailed?.(text)}
              >
                {t("conversation.message.resend")}
              </button>
            )}
            {unconfirmed && actions?.onCheckDelivery && <button type="button" className="kaus-msg-retry" onClick={actions.onCheckDelivery}>{c("chat.content.checkDelivery")}</button>}
          </div>
        )}
        {footer}
      </div>
    );
  }

  return (
    <div className="kaus-msg kaus-content-message is-assistant" data-phase={item.phase || undefined} data-actions-visible={visible} {...focusProps}>
      <div className="kaus-prose" data-testid="assistant-prose">
        <MarkdownBoundary text={text}>
          <MarkdownContent text={text} conversationId={conversationId} />
        </MarkdownBoundary>
        {streaming && <StreamCursor />}
        {!text && !streaming && <span className="kaus-prose-empty">{t("card.text.empty")}</span>}
      </div>
      {footer}
    </div>
  );
}
