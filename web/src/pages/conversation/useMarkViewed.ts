import { useEffect, useRef, type MutableRefObject } from "react";

import { useLocale } from "../../i18n";
import { markConversationViewed } from "../../lib/sessionApi";

const INTERVAL_MS = 1500;

/* 会话页开着、窗口在前台且有焦点时，把看到的最新事件序号报给后端。侧栏蓝点据此消失；
   后端也据此判断要不要发系统通知（切到别的应用时不上报，于是会收到通知）。
   只在序号前进时发请求；失败不提示，下一轮再试。 */
export function useMarkViewed(conversationId: string, lastSequenceRef: MutableRefObject<number | null>) {
  const { locale } = useLocale();
  const sent = useRef<number>(-1);
  useEffect(() => {
    sent.current = -1;
    const report = () => {
      const sequence = lastSequenceRef.current;
      if (document.visibilityState !== "visible" || !document.hasFocus() || sequence === null || sequence <= sent.current) return;
      sent.current = sequence;
      markConversationViewed(conversationId, sequence, locale).catch(() => { sent.current = -1; });
    };
    report();
    const timer = window.setInterval(report, INTERVAL_MS);
    document.addEventListener("visibilitychange", report);
    window.addEventListener("focus", report);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", report);
      window.removeEventListener("focus", report);
    };
  }, [conversationId, lastSequenceRef, locale]);
}
