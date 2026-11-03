import { FileText } from "lucide-react";

import { useLocale } from "../../i18n";
import { MessageDeliveryStatus } from "../../components/cards/MessageDeliveryStatus";
import { attachmentName } from "../../lib/attachmentsApi";
import type { PendingMessage } from "../../lib/pendingMessages";

/* 占位气泡（AD-94）：发送时先本地插一条，事件按 `clientRef` 回来后撤掉。
 * Local submissions and persisted messages share receipt controls. */

export interface PendingMessageRowsProps {
  pending: PendingMessage[];
  /** 「重发」禁用的条件由页面算好传进来（运行中 / 停止中 / 改设置中 / 只读）。 */
  retryDisabled: boolean;
  /** 运行中 / 停止中时「重发」的提示换成「等本轮结束」。 */
  retryWaiting: boolean;
  /** 正在查收据的那条 clientRef。 */
  checkingDelivery: string | null;
  onResend: (item: PendingMessage) => void;
  onCheckDelivery: (clientRef: string) => void;
}

export function PendingMessageRows({ pending, retryDisabled, retryWaiting, checkingDelivery, onResend, onCheckDelivery }: PendingMessageRowsProps) {
  const { t } = useLocale();
  return (
    <>
      {pending.map((item) => (
        <div
          key={item.clientRef}
          data-testid="pending-message"
          data-status={item.status}
          className={`kaus-msg kaus-content-message is-user ${item.status === "failed" ? "is-failed is-undelivered" : item.status === "sending" ? "is-pending" : "is-delivery-unconfirmed"}`}
        >
          <div className="kaus-msg-body">{item.text}</div>
          {!!item.attachments?.length && <div className="kaus-pending-attachments">{item.attachments.map(file => <span key={file.ref}><FileText size={14} />{attachmentName(file)}</span>)}</div>}
          {item.status !== "sending" && <div className="kaus-msg-footer">
            <MessageDeliveryStatus state={item.status}
              label={t(item.status === "failed" ? "conversation.pending.failed" : "conversation.pending.unconfirmed")}
              recovery={item.status === "failed" ? {
                label: t("conversation.pending.retry"),
                title: t(retryWaiting ? "conversation.message.resend.wait" : "conversation.pending.retry"),
                disabled: retryDisabled,
                onClick: () => onResend(item),
              } : {
                label: t("conversation.pending.check"),
                disabled: checkingDelivery === item.clientRef,
                onClick: () => onCheckDelivery(item.clientRef),
              }} />
          </div>}
        </div>
      ))}
    </>
  );
}
