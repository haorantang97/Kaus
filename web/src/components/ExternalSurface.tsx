/* 双表面（Card ⇄ External CLI）的界面件（Phase 4，任务书第 2/3/4/5 件）。
 *
 * 五个件，全部中性（DESIGN ★H：会话工作区里强调色只剩运行点与审批边框）：
 *  - `ExternalBanner`      外部态横幅 +「回到站内」
 *  - `DegradedLaunchPanel` `launched=false` 时的小面板：原因、重试与终端设置
 *  - `NoticeBanner`        接管提示那条中性横幅（5s 自动收起由页面控制）
 *  - `LaunchHistoryMenu`   页头「历史 ▾」，只在 `launches.count > 0` 时出现
 *  - `ExternalHistoryGroup` 回站后那组折叠的历史行
 *
 * 视觉一律复用现有 class（`.kaus-stream-bar` / `.kaus-bar-pill` / `.kaus-tool*` /
 * `.kaus-msg`），不新造颜色、不新造圆角（AD-78）。`complete=false` 的那行小字
 * **不用 `--danger`**——那是降级不是错误。
 */

import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, RotateCw, Settings2, Terminal, X } from "lucide-react";

import { useLocale } from "../i18n";
import { Button, Modal } from "./ui";
import type { ExternalHistoryGroup as HistoryGroup, ReconciledEntry } from "../lib/externalSurface";
import type { TerminalLaunchWire } from "../lib/sessionApi";

/** `HH:MM`；解析不了就不显示（不编一个）。 */
export function shortTime(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return undefined;
  return `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
}

/** 启动记录那一列的时间：`MM-DD HH:MM`（同一天里连开几次要分得清）。 */
export function launchTime(value: string | null | undefined): string {
  if (!value) return "—";
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/* ------------------------------------------------------------------ *
 * 外部态横幅（第 2 件）
 * ------------------------------------------------------------------ */

export function ExternalBanner({
  launchedAt,
  launcher,
  busy = false,
  onReturn,
}: {
  launchedAt: string | null;
  launcher: string | null;
  busy?: boolean;
  onReturn: () => void;
}) {
  const { t } = useLocale();
  const time = shortTime(launchedAt);
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
      </span>
      <button type="button" className="kaus-bar-pill is-button" disabled={busy} onClick={onReturn}>
        {t("surface.action.returnToCard")}
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * 「回到站内」的说明弹窗（批次十六第 2 件 / 走查 F3）
 * ------------------------------------------------------------------ */

/** 外部端还活着时，点「回到站内」先弹这张：说清楚两条路各是什么意思。
 *
 * 这不是加摩擦，是**给语义**——走查里用户点完不知道终端里的任务是不是被杀了。
 *  - 「回到页面」：只切视图，不调 `surface/card`，终端继续持有写权；
 *  - 「强制收回写权」：带 `force=1`，二次确认已经含在这张弹窗里，不再多问一次。 */
export function ReturnToAppDialog({
  busy = false,
  onStay,
  onForce,
  onCancel,
}: {
  busy?: boolean;
  onStay: () => void;
  onForce: () => void;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  return (
    <Modal onClose={onCancel} width={460}>
      <div className="kaus-return-dialog" data-testid="return-dialog">
        <p>{t("surface.return.body")}</p>
        <div className="kaus-return-actions">
          <Button onClick={onCancel}>{t("ui.cancel")}</Button>
          <Button onClick={onStay} disabled={busy}>
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
      {reason && <p className="kaus-external-degraded-reason">{reason}</p>}
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
 * 启动记录（第 5 件）
 * ------------------------------------------------------------------ */

export function LaunchHistoryMenu({ launches }: { launches: TerminalLaunchWire[] }) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // 一次也没开过就整枚不渲染（AD-71：不留占位、不写"暂无记录"）。
  if (launches.length === 0) return null;

  return (
    <span className="kaus-bar-menu" ref={ref} data-testid="launch-history">
      <button
        type="button"
        className="kaus-bar-pill is-button"
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((value) => !value)}
      >
        {t("surface.launches.label")}
        <span className="kaus-bar-caret" aria-hidden>
          ▾
        </span>
      </button>
      {open && (
        <span className="kaus-bar-pop" role="listbox" aria-label={t("surface.launches.label")}>
          {launches.slice(0, 5).map((launch) => (
            <span key={launch.id} role="option" aria-selected={false} className="kaus-bar-pop-item is-static">
              {[
                launchTime(launch.launchedAt),
                launch.launcher,
                launch.exitStatus === null
                  ? t("surface.launches.running")
                  : t("surface.launches.exit", { code: launch.exitStatus }),
              ].join(" · ")}
            </span>
          ))}
        </span>
      )}
    </span>
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
      {/* 降级不是错误：中性小字，常显（不藏在折叠里——它说明的正是"你看到的不全"）。 */}
      {!group.complete && (
        <div className="kaus-external-gap" data-testid="external-history-gap">
          {t("surface.history.incomplete", { gaps: group.gaps.join(", ") })}
        </div>
      )}
      {open && (
        <div className="kaus-tool-group-body kaus-external-history-body">
          {group.entries.map((entry) => (
            <HistoryEntry key={entry.entryId} entry={entry} />
          ))}
        </div>
      )}
    </div>
  );
}
