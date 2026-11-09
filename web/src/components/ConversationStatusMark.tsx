import { AlertCircle } from "lucide-react";

import { useLocale } from "../i18n";
import type { ConversationStatus } from "../lib/conversationIndex";

/* 侧栏行首的状态位（产品决策「会话状态显示」）：等待处理是橙点，已结束未查看是蓝点，
   失败是红色感叹号；运行中不放点，由标题流光表示。没有状态时留空位，标题保持对齐。
   不加文字，悬停与读屏用状态名。 */
export function ConversationStatusMark({ status }: { status: ConversationStatus | null }) {
  const { t } = useLocale();
  if (status === null) return <span className="kaus-status-slot" aria-hidden="true" />;
  const label = t(status === "running" ? "sidebar.running" : `sidebar.status.${status}`);
  return (
    <span className="kaus-status-slot" role="img" title={label} aria-label={label} data-status={status}>
      {status === "failed" ? <AlertCircle className="kaus-status-alert" aria-hidden="true" />
        : status === "running" ? null : <span className={`kaus-status-dot is-${status}`} />}
    </span>
  );
}
