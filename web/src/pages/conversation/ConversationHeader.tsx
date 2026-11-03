import { useEffect, useRef, useState } from "react";
import { Archive, ArchiveRestore, Ellipsis, GitBranch, Globe, Terminal } from "lucide-react";

import { useLocale } from "../../i18n";
import { UsagePill } from "../../components/cards/UsageBar";
import type { TimelineState } from "../../lib/timeline/reducer";

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
  /** 返回 false = 这一下被忽略了（正在归档 / 发送 / 运行中），菜单不收。 */
  onChangeArchive: (next: boolean) => boolean;
  /** 「继续到新会话」入口：没有回调就不渲染这一项。 */
  onOpenContinuation: (() => void) | undefined;
  /** 归档项的禁用条件由页面算好传进来。 */
  archiveDisabled: boolean;
  /** 归档项的提示：运行中 / 外部态时说「先停止」。 */
  archiveTitle: string | undefined;
  continuationDisabled: boolean;
  /** 侧边浏览器开关：只在带浏览器桥的 Chromium 壳里渲染（没有就不传）。 */
  browser?: { open: boolean; onToggle: () => void };
}

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
  archiveDisabled,
  archiveTitle,
  continuationDisabled,
  browser,
}: ConversationHeaderProps) {
  const { t } = useLocale();
  const [actionsOpen, setActionsOpen] = useState(false);
  const actionsRef = useRef<HTMLSpanElement | null>(null);
  /** batch52 第 5 件：正在就地改会话标题（null = 没在改）。 */
  const [titleDraft, setTitleDraft] = useState<string | null>(null);

  useEffect(() => {
    if (!actionsOpen) return;
    const dismiss = (event: MouseEvent) => {
      if (!actionsRef.current?.contains(event.target as Node)) setActionsOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setActionsOpen(false); };
    document.addEventListener("mousedown", dismiss);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("mousedown", dismiss); document.removeEventListener("keydown", escape); };
  }, [actionsOpen]);

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
              if (event.key === "Escape") {
                setTitleDraft(null);
                return;
              }
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
          <span
            className={`kaus-status ${running ? "is-running" : ""}`}
            data-testid="conversation-status"
            title={statusTitle}
          >
            {/* H-1：强调色在会话页只剩两处，这是其一（运行状态点）。 */}
            {running && <span className="kaus-run-dot kaus-accent" data-accent="run" />}
            {archived ? t("conversation.archived") : statusText}
          </span>
          {/* G-8：用量折成一枚 Pill，挂在页头右侧；没有 usage 能力就不渲染。 */}
          {showUsage && <UsagePill usage={usage} />}
          {browser && (
            <button type="button" className="kaus-chat-icon-action" aria-pressed={browser.open}
              title={browser.open ? t("browser.close") : t("browser.open")} aria-label={browser.open ? t("browser.close") : t("browser.open")}
              onClick={browser.onToggle}><Globe size={16} aria-hidden /></button>
          )}
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
              <Terminal size={16} aria-hidden />
            </button>
          )}
          {archived ? (
            <button type="button" className="kaus-chat-icon-action" title={t("conversation.restore")} aria-label={t("conversation.restore")}
              disabled={archiveBusy} onClick={() => onChangeArchive(false)}><ArchiveRestore size={16} aria-hidden /></button>
          ) : (
            <span className="kaus-conversation-actions" ref={actionsRef}>
              <button type="button" className="kaus-chat-icon-action" title={t("conversation.actions")} aria-label={t("conversation.actions")}
                aria-haspopup="menu" aria-expanded={actionsOpen} onClick={() => setActionsOpen(value => !value)}><Ellipsis size={17} aria-hidden /></button>
              {actionsOpen && <span className="kaus-bar-pop" role="menu" aria-label={t("conversation.actions")}>
                {onOpenContinuation && <button type="button" role="menuitem" className="kaus-bar-pop-item" disabled={continuationDisabled}
                  onClick={() => { setActionsOpen(false); onOpenContinuation(); }}><GitBranch size={14} aria-hidden />{t("continuation.title")}</button>}
                <button type="button" role="menuitem" className="kaus-bar-pop-item" disabled={archiveDisabled}
                  title={archiveTitle} onClick={() => { if (onChangeArchive(true)) setActionsOpen(false); }}>
                  <Archive size={14} aria-hidden />{t("conversation.archive")}
                </button>
              </span>}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
