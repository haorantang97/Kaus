import { useState } from "react";
import { ArchiveRestore, FileDiff, Folder, GitBranch, Globe, SquareArrowOutUpRight, Terminal } from "lucide-react";

import { useLocale } from "../../i18n";
import { useDraftEscape } from "../../hooks/useDraftEscape";
import { UsagePill } from "../../components/cards/UsageBar";
import type { TimelineState } from "../../lib/timeline/reducer";
import type { SidePanelKind } from "../../lib/sidePanel";

/* 对话页页头：标题（就地改名）、运行状态、用量 Pill、「到终端打开」与 ⋯ 菜单。
 * 页头背景横跨全宽，内容对齐 760 栏（G-1）。 */

export interface ConversationHeaderProps {
  /** 当前标题；null = 还没读到（显示回退文案）。 */
  title: string | null | undefined;
  /** Enter 存：非空且与当前不同时才会调到这里。 */
  onRename: (title: string) => void;
  running: boolean;
  archived: boolean;
  statusText: string;
  /** 离线原因是人话，挂在状态的 title 上（页头不因此多一枚 chip）。 */
  statusTitle: string | undefined;
  /** G-8：用量折成一枚 Pill；没有 usage 能力就不渲染。 */
  showUsage: boolean;
  usage: TimelineState["usage"];
  canOpenExternal: boolean;
  handoffBusy: boolean;
  onOpenExternal: () => void;
  archiveBusy: boolean;
  /** Restore of an archived conversation; archive and delete live in the sidebar row menu (2026-11-18). */
  onChangeArchive: (next: boolean) => boolean;
  /** 「用其他 Agent 接续」 as a header icon (2026-11-18); also in the sidebar row menu. */
  onOpenContinuation?: () => void;
  continuationDisabled?: boolean;
  /** Panel switches (2026-11-22, same order as the reference desktop app: Changes, Browser, Files, Terminal). One slot:
   *  pressing another icon replaces the panel, pressing the open one closes it. */
  panels?: { current: SidePanelKind | null; available: Record<SidePanelKind, boolean>; onToggle: (kind: SidePanelKind) => void };
}

const PANEL_ICONS = [["changes", FileDiff], ["browser", Globe], ["files", Folder], ["terminal", Terminal]] as const;

export function ConversationHeader({
  title,
  onRename,
  running,
  archived,
  statusText,
  statusTitle,
  showUsage,
  usage,
  canOpenExternal,
  handoffBusy,
  onOpenExternal,
  archiveBusy,
  onChangeArchive,
  onOpenContinuation,
  continuationDisabled,
  panels,
}: ConversationHeaderProps) {
  const { t } = useLocale();
  /** batch52 第 5 件：正在就地改会话标题（null = 没在改）。 */
  const [titleDraft, setTitleDraft] = useState<string | null>(null);
  const draftEscape = useDraftEscape();

  return (
    <header className="kaus-conversation-header">
      <div className="kaus-thread kaus-conversation-headline">
        {/* batch52 第 5 件：会话标题**此前没有任何界面入口**（侧栏 ⋯ 菜单里那条
            「改名」走的是 `POST /api/agent/{name}/rename`，改的是项目的显示名）。
            入口就放在标题本身上：点一下就地改，Enter 存、Esc 收——页头不因此多一枚
            按钮（★H：这一片是中性的工作区，强调色只给运行点）。 */}
        {titleDraft === null ? (
          <button
            type="button"
            className="kaus-conversation-title"
            data-testid="conversation-title"
            title={t("conversation.title.rename")}
            onClick={() => setTitleDraft(title ?? "")}
          >
            {title ?? t("conversation.title.fallback")}
          </button>
        ) : (
          <input
            autoFocus
            className="kaus-conversation-title kaus-conversation-title-input"
            aria-label={t("conversation.title.field")}
            data-testid="conversation-title-input"
            value={titleDraft}
            onChange={(event) => setTitleDraft(event.target.value)}
            onBlur={() => setTitleDraft(null)}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (draftEscape(event, titleDraft !== (title ?? ""), () => setTitleDraft(null))) return;
              if (event.key !== "Enter") return;
              event.preventDefault();
              const next = titleDraft.trim();
              setTitleDraft(null);
              // 空标题不发：后端会 400 `empty_title`，但用户按 Enter 想要的是
              // 「算了」，不是一条错误提示。
              if (!next || next === title) return;
              onRename(next);
            }}
          />
        )}
        <div className="kaus-conversation-meta">
          {/* 2026-11-18：「空闲」不显示，页面传空串；其余状态照常。 */}
          {(archived || statusText) && <span
            className={`kaus-status ${running ? "is-running" : ""}`}
            data-testid="conversation-status"
            title={statusTitle}
          >
            {/* H-1：强调色在会话页只剩两处，这是其一（运行状态点）。 */}
            {running && <span className="kaus-run-dot kaus-accent" data-accent="run" />}
            {archived ? t("conversation.archived") : statusText}
          </span>}
          {/* G-8：用量折成一枚 Pill，挂在页头右侧；没有 usage 能力就不渲染。 */}
          {showUsage && <UsagePill usage={usage} />}
          {panels && PANEL_ICONS.filter(([kind]) => panels.available[kind]).length > 0 && <span className="kaus-header-panels">
            {PANEL_ICONS.filter(([kind]) => panels.available[kind]).map(([kind, Icon]) => {
              const label = t(`panel.${kind}`);
              return <button key={kind} type="button" className="kaus-chat-icon-action" aria-pressed={panels.current === kind}
                data-testid={`panel-toggle-${kind}`} title={label} aria-label={label} onClick={() => panels.onToggle(kind)}>
                <Icon size={16} aria-hidden /></button>;
            })}
          </span>}
          {canOpenExternal && (
            <button
              type="button"
              className="kaus-chat-icon-action"
              data-testid="open-external"
              /* 运行中是**禁用**不是报错（第 1 件）：提示挂在 title 上。 */
              disabled={running || handoffBusy}
              title={running ? t("surface.action.stopFirst") : t("surface.action.openExternal")}
              aria-label={t("surface.action.openExternal")}
              onClick={onOpenExternal}
            >
              {/* 2026-11-23: the terminal glyph belongs to the Terminal panel; moving the conversation out uses this one. */}
              <SquareArrowOutUpRight size={16} aria-hidden />
            </button>
          )}
          {onOpenContinuation && !archived && (
            <button type="button" className="kaus-chat-icon-action" disabled={continuationDisabled}
              title={continuationDisabled ? t("surface.action.stopFirst") : t("continuation.title")} aria-label={t("continuation.title")}
              onClick={onOpenContinuation}><GitBranch size={16} aria-hidden /></button>
          )}
          {archived && (
            <button type="button" className="kaus-chat-icon-action" title={t("conversation.restore")} aria-label={t("conversation.restore")}
              disabled={archiveBusy} onClick={() => onChangeArchive(false)}><ArchiveRestore size={16} aria-hidden /></button>
          )}
        </div>
      </div>
    </header>
  );
}
