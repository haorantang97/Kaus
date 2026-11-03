import { useCallback, useEffect, useRef, useState } from "react";

/* 外壳级的"有变化了，重拉"信号（component-boundaries §4.1）。
 *
 * `EventSource("/api/events")` 收到 `state_changed` → 180ms 防抖 → 按 `scopes` 决定
 * 要不要重拉项目树，同时 `refreshKey += 1` 让各分区重取；`state_snapshot` 的版本号
 * 变了（重连期间漏了事件）也同样处理。页面重新可见时（会话页除外）对账一次。 */

interface DashboardEvent {
  type: string;
  scopes?: string[];
  version?: string;
  ts?: number;
}

/** 这些 scope 的变化会动到项目树，需要重拉 `/api/network`。 */
const NETWORK_SCOPES = ["network", "profiles", "skills", "config"];

export function useDashboardEvents({ sessionOn, reload, routeName }: {
  sessionOn: boolean;
  /** 重拉项目树（`App` 里的 `reload`，identity 稳定）。 */
  reload: () => void;
  /** 当前路由名：会话页自己有事件流，重新可见时不对账。 */
  routeName: string;
}) {
  const [refreshKey, setRefreshKey] = useState(0);
  const refreshTimer = useRef<number | null>(null);
  const stateVersion = useRef<string | null>(null);

  useEffect(() => {
    if (!sessionOn) return;
    const es = new EventSource("/api/events");
    const schedule = (event: DashboardEvent) => {
      if (event.version) {
        const changedSinceLastMessage = stateVersion.current !== null && stateVersion.current !== event.version;
        stateVersion.current = event.version;
        if (event.type === "state_snapshot" && changedSinceLastMessage) {
          reload();
          setRefreshKey((value) => value + 1);
        }
      }
      if (event.type !== "state_changed") return;
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      refreshTimer.current = window.setTimeout(() => {
        const scopes = new Set(event.scopes || []);
        if (NETWORK_SCOPES.some((scope) => scopes.has(scope))) reload();
        setRefreshKey((value) => value + 1);
      }, 180);
    };
    es.onmessage = (message) => {
      try { schedule(JSON.parse(message.data) as DashboardEvent); }
      catch { /* ignore malformed SSE chunks */ }
    };
    es.onerror = () => {
      // EventSource 自带重连；这里不弹 toast，避免后台文件频繁改动时打扰对话。
    };
    return () => {
      es.close();
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
    };
  }, [reload, sessionOn]);

  useEffect(() => {
    if (!sessionOn) return;
    const reconcile = () => {
      if (routeName === "conversation") return;
      void reload();
      setRefreshKey((value) => value + 1);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") reconcile();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [reload, routeName, sessionOn]);

  /** 手动触发一次"全都重取"（右键菜单改名之后）。 */
  const bump = useCallback(() => setRefreshKey((value) => value + 1), []);
  return { refreshKey, bump };
}
