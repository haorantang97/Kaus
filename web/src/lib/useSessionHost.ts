import { useEffect, useState } from "react";
import { bootstrapSessionAuth } from "./sessionApi";

/* 「会话功能开没开」的唯一判据：`GET /api/session-auth/bootstrap` 成不成功。
 *
 * 前端不读后端的 feature flag（那要么得多一个端点，要么得把配置搬到前端）；
 * flag 关闭时这条路由根本不存在，一次 404 就够回答这个问题。
 * 探测期间界面**保持旧样子**——这样 flag 关闭时看不出任何差别（验收点 1）。
 */

export type SessionHostStatus = "probing" | "available" | "unavailable";

export function useSessionHost(): { status: SessionHostStatus; error: string | null } {
  const [status, setStatus] = useState<SessionHostStatus>("probing");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    bootstrapSessionAuth()
      .then(() => {
        if (!cancelled) setStatus("available");
      })
      .catch((failure) => {
        if (cancelled) return;
        setError(String((failure as Error)?.message ?? failure));
        setStatus("unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { status, error };
}
