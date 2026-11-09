import { Clock, X } from "lucide-react";

import { useLocale } from "../../i18n";
import type { QueuedMessageWire } from "../../lib/sessionApi";

/* 输入框上方的排队消息：一行一条，可取消；用户停止后暂停，点「继续发送」再按顺序发。 */
export function QueuedMessages({ items, paused, onRemove, onSendNow }: {
  items: QueuedMessageWire[];
  paused: boolean;
  onRemove: (id: string) => void;
  onSendNow: () => void;
}) {
  const { t } = useLocale();
  return (
    <div className="kaus-queue" data-testid="message-queue" aria-label={t("conversation.queue.label")}>
      {items.map(item => (
        <div key={item.id} className="kaus-queue-row">
          <Clock size={13} aria-hidden="true" />
          <span className="kaus-queue-text" title={item.text}>{item.text}</span>
          <button type="button" className="kaus-cap-icon-button" aria-label={t("conversation.queue.remove")} title={t("conversation.queue.remove")}
            onClick={() => onRemove(item.id)}><X size={13} /></button>
        </div>
      ))}
      {paused && <button type="button" className="kaus-inline-action" onClick={onSendNow}>{t("conversation.queue.resume")}</button>}
    </div>
  );
}
