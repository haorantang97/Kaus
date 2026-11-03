import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { useLocale } from "../../i18n";
import { confirmAsync } from "../../components/ui";
import { hasCapability, type UiCapabilities } from "../../components/cards/capabilities";
import {
  fetchSurface,
  openExternalSurface,
  returnToCardSurface,
  SessionApiError,
  describeFailure,
  type ConversationDetailWire,
  type SurfaceLeaseWire,
  type TerminalLaunchWire,
} from "../../lib/sessionApi";
import {
  consumeExternalHandoff,
  readExternalHistory,
  setExternalSurface,
  writeExternalHistory,
  type ExternalHistoryGroup as HistoryGroup,
  type SurfaceEvent,
  type SurfaceKind,
} from "../../lib/externalSurface";

/* 双表面（Phase 4）：站内卡片 / 外部终端。这一组状态**只住在页面层**：reducer 是
 * 内核镜像，"谁在写"不是时间线内容（见 lib/externalSurface.ts 顶部）。
 *
 * 四件事：表面状态与租约、`kaus` 的三条表面事件、两条交接路径（到终端 / 回站内）、
 * 项目页送过来的交接标记。 */

export interface ConversationSurfaceOptions {
  conversationId: string;
  archived: boolean;
  archiveBusy: boolean;
  /** 到终端时把没发出去的草稿带过去（原生不支持预填就复制到剪贴板）。 */
  draftText: string;
  /** 能力没声明就不接项目页的交接标记（AD-71）。 */
  caps: UiCapabilities | null;
  onActivity?: () => void;
  setError: (error: string | null) => void;
  /** 回站内时把首帧兜底的 `runtime.owner` 清掉：卡片拿到写权后页面不能还是只读。 */
  setDetail: Dispatch<SetStateAction<ConversationDetailWire | null>>;
}

/** 409 `lease_held` / `external_active` 的 detail：`{owner, ownerId, acquiredAt, launchId}`。 */
function conflictDetail(failure: unknown, code: string): Record<string, unknown> | null {
  if (!(failure instanceof SessionApiError)) return null;
  if (failure.status !== 409 || failure.code !== code) return null;
  const detail = failure.detailFields.detail;
  return detail && typeof detail === "object" && !Array.isArray(detail)
    ? (detail as Record<string, unknown>)
    : {};
}

export function useConversationSurface({ conversationId, archived, archiveBusy, draftText, caps, onActivity, setError, setDetail }: ConversationSurfaceOptions) {
  const { t } = useLocale();
  const [surface, setSurface] = useState<SurfaceKind>("card");
  const [launch, setLaunch] = useState<TerminalLaunchWire | null>(null);
  /* A terminal lease remains authoritative until verified handoff completes. */
  const [lease, setLease] = useState<SurfaceLeaseWire | null>(null);
  const [returnOpen, setReturnOpen] = useState(false);
  /** `launched=false` 时那个小面板：降级原因 + 可复制的命令。 */
  const [degraded, setDegraded] = useState<{ reason: string | null } | null>(null);
  /** 接管提示（5s 自动收起）。 */
  const [notice, setNotice] = useState<string | null>(null);
  const [handoffBusy, setHandoffBusy] = useState(false);
  /* 折叠历史组跟着会话走：换会话时**在渲染期**重置（用 effect 会先用旧值把新会话
     那把存储覆盖掉）。历史落盘只在新增那一刻写，不挂 effect，理由同上。 */
  const [history, setHistory] = useState<{ id: string; groups: HistoryGroup[] }>(() => ({
    id: conversationId,
    groups: readExternalHistory(conversationId),
  }));
  if (history.id !== conversationId) {
    setHistory({ id: conversationId, groups: readExternalHistory(conversationId) });
  }

  /** 表面切换的唯一入口：**以事件/接口返回为准**，不做本地推测（第 4 件）。
   *  顺手登记给侧栏那枚小终端图标。 */
  const applySurface = useCallback(
    (next: SurfaceKind) => {
      setSurface(next);
      setExternalSurface(conversationId, next === "external-cli");
      if (next === "card") {
        setDegraded(null);
        setLease(null);
        // The first-load owner is only a fallback; once the card has the writer it must not keep the page read-only.
        setDetail(current => current?.runtime?.owner?.ownerType === "external-cli"
          ? { ...current, runtime: { ...current.runtime, owner: null } } : current);
      }
    },
    [conversationId],
  );

  /* 刷新恢复（第 2 件最后一句）：`GET /surface` 说了算。端点不在就是站内。 */
  useEffect(() => {
    let cancelled = false;
    setDegraded(null);
    setNotice(null);
    fetchSurface(conversationId)
      .then((state) => {
        if (cancelled) return;
        setLaunch(state.launch);
        setLease(state.lease);
        setSurface(state.surface);
        setExternalSurface(conversationId, state.surface === "external-cli");
      })
      .catch(() => {
        if (cancelled) return;
        setSurface("card");
        setLaunch(null);
        setLease(null);
        setExternalSurface(conversationId, false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  /** 新增一组折叠历史（回站校准的产物）。同一组重复到达时按 id 覆盖，不叠加。 */
  const addHistoryGroup = useCallback(
    (group: HistoryGroup) => {
      setHistory((current) => {
        const groups = [...current.groups.filter((item) => item.id !== group.id), group];
        writeExternalHistory(current.id, groups);
        return { id: current.id, groups };
      });
    },
    [],
  );

  const handleSurfaceEvent = useCallback(
    (event: SurfaceEvent) => {
      if (event.kind === "surface.changed") {
        // 终端自己退出时监视器也走这一条：页面不区分"谁触发的"。
        applySurface(event.surface);
        /* 第 2 件：租约跟着表面走一次真源（进外部态时它才有意义）。 */
        if (event.surface === "external-cli") {
          fetchSurface(conversationId)
            .then((state) => setLease(state.lease))
            .catch(() => setLease(null));
        }
        return;
      }
      if (event.kind === "history.reconciled") {
        if (event.entries.length === 0 && event.complete) return;
        addHistoryGroup({
          id: `reconciled:${event.entries[0]?.entryId ?? "empty"}:${event.entries.length}`,
          entries: event.entries,
          complete: event.complete,
          gaps: event.gaps,
        });
        return;
      }
      setNotice(
        t("surface.notice.takenOver", {
          owner: event.previousOwnerLabel ?? event.previousOwner ?? t("surface.owner.unknown"),
          reason: event.reason ?? t("surface.reason.unknown"),
        }),
      );
    },
    [addHistoryGroup, applySurface, conversationId, t],
  );

  /* SSE 订阅只按 conversationId 建一次，所以处理函数走 ref——换了它不该重连。 */
  const surfaceHandlerRef = useRef(handleSurfaceEvent);
  surfaceHandlerRef.current = handleSurfaceEvent;
  const dispatchSurfaceEvent = useCallback((event: SurfaceEvent) => surfaceHandlerRef.current(event), []);

  /* 接管提示 5s 自动收起（第 4 件）。 */
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 5_000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  /** 到终端打开（第 1 件）。`lease_held` → 二次确认后带 `force=1` 重试一次。 */
  const toExternal = useCallback(async () => {
    if (archived || archiveBusy) return;
    setHandoffBusy(true);
    try {
      for (let force = false; ; force = true) {
        try {
          const unsent = draftText.trim() ? draftText : undefined;
          const result = await openExternalSurface(conversationId, { ...(force ? { force: true } : {}), draft: unsent });
          setError(null);
          // 草稿留在 Kaus；原生不支持预填时复制到剪贴板，不往终端注入按键。
          if (unsent && result.launched && !result.draftPrefilled) {
            const copied = await navigator.clipboard?.writeText(unsent).then(() => true, () => false);
            setNotice(t(copied ? "surface.draft.copied" : "surface.draft.kept"));
          }
          setLaunch(result.launch);
          applySurface(result.launched && result.surface === "external-cli" ? "external-cli" : "card");
          setLease(result.lease);
          setDegraded(
            result.launched
              ? null
              : {
                  reason: result.reason,
                },
          );
          onActivity?.();
          return;
        } catch (failure) {
          const held = conflictDetail(failure, "lease_held");
          if (held && !force) {
            const ok = await confirmAsync(
              t("surface.confirm.leaseHeld", {
                owner: String(held.owner ?? t("surface.owner.unknown")),
                acquiredAt: String(held.acquiredAt ?? t("surface.reason.unknown")),
              }),
              { okLabel: t("surface.confirm.takeover"), danger: true },
            );
            if (!ok) return;
            continue;
          }
          // 501 与其余错误：原样显示后端 message（前端不改写）。
          setError(describeFailure(failure));
          return;
        }
      }
    } finally {
      setHandoffBusy(false);
    }
  }, [applySurface, archived, archiveBusy, conversationId, onActivity, t, draftText]);

  /** 回到站内（第 3 件）。`external_active` → 确认后 `force=1`。
   *  折叠历史组由 SSE 的 `history.reconciled` 送来，这里只负责切态。 */
  const toCard = useCallback(async (options: { force?: boolean } = {}) => {
    setReturnOpen(false);
    setHandoffBusy(true);
    try {
      for (let force = options.force ?? false; ; force = true) {
        try {
          await returnToCardSurface(conversationId, force ? { force: true } : {});
          setError(null);
          applySurface("card");
          onActivity?.();
          return;
        } catch (failure) {
          const active = conflictDetail(failure, "external_active");
          if (active && !force) {
            const ok = await confirmAsync(t("surface.confirm.externalActive"), {
              okLabel: t("surface.confirm.reclaim"),
              danger: true,
            });
            if (!ok) return;
            continue;
          }
          setError(describeFailure(failure));
          return;
        }
      }
    } finally {
      setHandoffBusy(false);
    }
  }, [applySurface, conversationId, onActivity, t]);

  /* Confirm before stopping an active terminal; ordinary page viewing needs no handoff. */
  const externalActive = lease?.owner === "external-cli";
  const requestReturn = useCallback(() => {
    if (externalActive) {
      setReturnOpen(true);
      return;
    }
    void toCard();
  }, [externalActive, toCard]);

  /* 第 5 件：项目页那枚「到终端打开」把用户送到这里之后，接着触发第 1 件。
     标记读一次即焚；能力没声明就什么也不做（AD-71）。 */
  useEffect(() => {
    if (!caps || !hasCapability(caps.externalCli.supported)) return;
    const consume = () => {
      if (!consumeExternalHandoff(conversationId)) return;
      void toExternal();
    };
    consume();
    // Group actions can target the conversation that is already on screen.
    window.addEventListener("kaus:external-handoff", consume);
    return () => window.removeEventListener("kaus:external-handoff", consume);
  }, [caps, conversationId, toExternal]);

  return {
    surface,
    launch,
    lease,
    returnOpen,
    setReturnOpen,
    degraded,
    setDegraded,
    notice,
    setNotice,
    handoffBusy,
    history,
    applySurface,
    dispatchSurfaceEvent,
    toExternal,
    toCard,
    requestReturn,
  };
}
