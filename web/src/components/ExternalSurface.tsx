/* 双表面（Card ⇄ External CLI）的界面件（Phase 4，任务书第 2/3/4/5 件）。
 *
 * 四个件，全部中性（DESIGN ★H：会话工作区里强调色只剩运行点与审批边框）：
 *  - `ExternalBanner`      外部态横幅 +「回到站内」
 *  - `DegradedLaunchPanel` `launched=false` 时的小面板：原因、重试与终端设置
 *  - `NoticeBanner`        接管提示那条中性横幅（5s 自动收起由页面控制）
 *  - `ExternalHistoryGroup` 回站后那组折叠的历史行
 *
 * 视觉一律复用现有 class（`.kaus-stream-bar` / `.kaus-bar-pill` / `.kaus-tool*` /
 * `.kaus-msg`），不新造颜色、不新造圆角（AD-78）。`complete=false` 的那行小字
 * **不用 `--danger`**——那是降级不是错误。
 */

import { useState } from "react";
import { ChevronDown, ChevronRight, RotateCw, Settings2, SquareArrowOutUpRight, Terminal, X } from "lucide-react";

import { useLocale } from "../i18n";
import { Button, ErrorNotice, Modal } from "./ui";
import { failureDiagnostic } from "../lib/userFacingErrors";
import type { ExternalHistoryGroup as HistoryGroup, ReconciledEntry } from "../lib/externalSurface";
import type { TerminalStatusWire } from "../lib/sessionApi";

/** `HH:MM`；解析不了就不显示（不编一个）。 */
export function shortTime(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return undefined;
  return `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
}

/* ------------------------------------------------------------------ *
 * 外部态横幅（第 2 件）
 * ------------------------------------------------------------------ */

export function ExternalBanner({
  launchedAt,
  launcher,
  busy = false,
  status = null,
  onReturn,
  onFocus,
}: {
  launchedAt: string | null;
  launcher: string | null;
  busy?: boolean;
  /** hook 上报的终端状态；没有就不显示（不猜）。 */
  status?: TerminalStatusWire | null;
  onReturn: () => void;
  onFocus?: () => void;
}) {
  const { t } = useLocale();
  const time = shortTime(launchedAt);
  const waiting = status?.state === "waiting";
  return (
    <div className="kaus-stream-bar kaus-external-banner" data-testid="external-banner">
      <Terminal className="size-3.5 shrink-0" aria-hidden />
      <span className="flex-1">
        {/* 启动时间 / launcher 缺一个就少一段，不显示占位符。 */}
        {[
          t("surface.external.banner"),
          time ? t("surface.external.since", { time }) : null,
          launcher,
        ]
          .filter(Boolean)
          .join(" · ")}
        {status && <span className="kaus-external-status" data-testid="terminal-status" data-state={status.state}
          title={status.message ?? undefined}> · {t(`surface.status.${status.state}`)}</span>}
      </span>
      {onFocus && <button type="button" className={`kaus-bar-pill is-button${waiting ? " kaus-accent" : ""}`}
        aria-label={t("surface.action.focus")} title={t("surface.action.focus")} onClick={onFocus}>
        <SquareArrowOutUpRight className="size-3.5" aria-hidden />
      </button>}
      <button type="button" className="kaus-bar-pill is-button" disabled={busy} onClick={onReturn}>
        {t("surface.action.returnToCard")}
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 「回到站内」的说明弹窗（批次十六第 2 件 / 走查 F3）
 * ------------------------------------------------------------------ */

/** Keep the terminal active, or stop its agent before returning control to Kaus. */
export function ReturnToAppDialog({
  busy = false,
  onForce,
  onCancel,
}: {
  busy?: boolean;
  onForce: () => void;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  return (
    <Modal onClose={onCancel} width={460}>
      <div className="kaus-return-dialog" data-testid="return-dialog">
        <p>{t("surface.return.body")}</p>
        <div className="kaus-return-actions">
          <Button onClick={onCancel} disabled={busy}>
            {t("surface.return.stay")}
          </Button>
          {/* 破坏性的那条走 danger 描边（现有 token，不新造颜色）。 */}
          <Button variant="danger" onClick={onForce} disabled={busy}>
            {t("surface.return.force")}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------ *
 * 降级面板（第 1 件的 `launched=false` 分支）
 * ------------------------------------------------------------------ */

export function DegradedLaunchPanel({
  reason,
  busy = false,
  onRetry,
  onDismiss,
}: {
  reason: string | null;
  busy?: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const { t } = useLocale();
  return (
    <div className="kaus-external-degraded" data-testid="degraded-panel">
      <div className="kaus-external-degraded-head">
        <span>{t("surface.degraded.title")}</span>
        <button type="button" className="kaus-bar-pill is-button" disabled={busy}
          title={t("card.error.retry")} aria-label={t("card.error.retry")} onClick={onRetry}>
          <RotateCw className="size-3.5" aria-hidden />
        </button>
        <a href="/config" className="kaus-bar-pill is-button"
          title={t("nav.config")} aria-label={t("nav.config")}>
          <Settings2 className="size-3.5" aria-hidden />
        </a>
        <button type="button" className="kaus-bar-pill is-button"
          title={t("surface.degraded.dismiss")} aria-label={t("surface.degraded.dismiss")} onClick={onDismiss}>
          <X className="size-3.5" aria-hidden />
        </button>
      </div>
      {reason && <ErrorNotice error={reason} className="kaus-external-degraded-reason" />}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 中性提示横幅（第 4 件的接管提示）
 * ------------------------------------------------------------------ */

export function NoticeBanner({ text, testId }: { text: string; testId: string }) {
  return (
    <div className="kaus-stream-bar" data-testid={testId} role="status">
      {text}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 折叠的历史组（第 3 件）
 * ------------------------------------------------------------------ */

function HistoryEntry({ entry }: { entry: ReconciledEntry }) {
  if (entry.role === "user") {
    return (
      <div className="kaus-msg is-user">
        <div className="kaus-msg-body">{entry.text}</div>
      </div>
    );
  }
  // 助手侧沿用不装盒的正文（G-2）。原生历史是纯文本，不过 Markdown 渲染。
  return (
    <div className="kaus-msg is-assistant">
      <div className="kaus-prose">{entry.text}</div>
    </div>
  );
}

/** 终端运行期间的新消息，浅色显示；回站校准后由正式记录替换。 */
export function TerminalPreview({ entries }: { entries: ReconciledEntry[] }) {
  const { t } = useLocale();
  if (!entries.length) return null;
  return (
    <div className="kaus-terminal-preview" data-testid="terminal-preview" aria-live="polite">
      <div className="kaus-tool-name">{t("surface.preview.label", { count: entries.length })}</div>
      {entries.map((entry) => <HistoryEntry key={entry.entryId} entry={entry} />)}
    </div>
  );
}

export function ExternalHistoryGroup({ group }: { group: HistoryGroup }) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="kaus-tool-group" data-testid="external-history-group">
      <button
        type="button"
        className="kaus-tool-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Chevron className="size-3 kaus-tool-chevron" />
        <span className="kaus-tool-name">
          {t("surface.history.group", { count: group.entries.length })}
        </span>
      </button>
      {open && (
        <div className="kaus-tool-group-body kaus-external-history-body">
          {group.entries.map((entry) => (
            <HistoryEntry key={entry.entryId} entry={entry} />
          ))}
          {!group.complete && group.gaps.length > 0 && <details className="text-xs opacity-60" data-testid="external-history-details">
            <summary>{t("card.error.showDetail")}</summary>
            <pre className="mt-2 whitespace-pre-wrap">{failureDiagnostic(group.gaps.join("\n"))}</pre>
          </details>}
        </div>
      )}
    </div>
  );
}
