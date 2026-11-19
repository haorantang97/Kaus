import { AlertCircle } from "lucide-react";

import { useLocale } from "../i18n";
import type { ConversationStatus } from "../lib/conversationIndex";

/* 侧栏行首的状态位（产品决策「会话状态显示」）：等待处理是橙色菱形，已结束未查看是蓝点，
   失败是红色感叹号；运行中由标题流光表示，减弱动效时改为一个空心圈。没有状态时留空位，标题保持对齐。
   不加文字，悬停与读屏用状态名。 */
export function ConversationStatusMark({ status }: { status: ConversationStatus | null }) {
  const { t } = useLocale();
  if (status === null) return <span className="kaus-status-slot" aria-hidden="true" />;
  const label = t(status === "running" ? "sidebar.running" : `sidebar.status.${status}`);
  return (
    <span className="kaus-status-slot" role="img" title={label} aria-label={label} data-status={status}>
      {/* Unread is a blue dot before the title; awaiting is an orange dot after it (2026-11-19).
          Running shows a ring only when reduced motion turns off the title shimmer. */}
      {status === "failed" ? <AlertCircle className="kaus-status-alert" aria-hidden="true" />
        : <span className={`kaus-status-dot is-${status}`} />}
    </span>
  );
}
