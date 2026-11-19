import { useRef, useState, type ReactNode } from "react";
import { ArrowRightLeft, Unlink } from "lucide-react";
import { useLocale } from "../i18n";
import { archiveConversation, deleteConversation, patchConversation, type ConversationIndexEntryWire } from "../lib/sessionApi";
import { readSidebarPins, saveSidebarPins } from "../lib/sidebarPins";
import { notifyHistoryChanged, projectHistoryApi } from "../lib/projectHistory";
import type { ArchiveItem } from "../lib/nativeArchivesApi";
import { ConversationRowMenu, RowMenu, type RowMenuPoint } from "./ConversationRowMenu";
import { ContinuationDialog } from "./ContinuationDialog";
import { confirmAsync, toast, undoToast } from "./ui";
import { useDraftEscape } from "../hooks/useDraftEscape";

/* 对话行在各处一致（2026-11-17，product-decisions「入口与用词（续）」之后的第 7 条）：单击打开，
   行 ⋯ 与右键是同一份菜单。侧栏对话行是标准；这里给项目页对话行和原生历史行用。 */

function usePoint() {
  const [point, setPoint] = useState<RowMenuPoint | null>(null);
  const seq = useRef(0);
  const onContextMenu = (event: React.MouseEvent) => {
    event.preventDefault();
    seq.current += 1;
    setPoint({ x: event.clientX, y: event.clientY, seq: seq.current });
  };
  return { point, onContextMenu };
}

/** A Kaus conversation row outside the sidebar: same menu as the sidebar (pin, rename, archive, delete). */
export function ConversationRow({ conversation, className, onOpen, onOpenConversation, onChanged, children }: {
  conversation: ConversationIndexEntryWire;
  className: string;
  onOpen: () => void;
  /** Opens the new conversation that 「用其他 Agent 接续」 creates. */
  onOpenConversation?: (conversationId: string) => void;
  /** After rename, archive or delete, so the list reloads. */
  onChanged: () => void;
  children: ReactNode;
}) {
  const { t } = useLocale();
  const { point, onContextMenu } = usePoint();
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const draftEscape = useDraftEscape();
  const [pins, setPins] = useState(readSidebarPins);
  const [continuing, setContinuing] = useState(false);
  const project = conversation.projectId;
  const pinned = (pins[project] ?? []).includes(conversation.id);
  const running = conversation.status === "running";
  const unpin = () => { const next = { ...pins, [project]: (pins[project] ?? []).filter(id => id !== conversation.id) }; setPins(next); saveSidebarPins(next); };
  const work = async (call: () => Promise<unknown>, failure: string) => {
    setBusy(true);
    // The sidebar list follows through the shared refresh event.
    try { await call(); onChanged(); window.dispatchEvent(new Event("kaus:conversations-changed")); return true; }
    catch { toast(t(failure as "sidebar.renameFailed"), "bad"); return false; }
    finally { setBusy(false); }
  };
  const rename = async () => {
    const title = (renaming ?? "").trim();
    if (!title || title === conversation.title) { setRenaming(null); return; }
    if (await work(() => patchConversation(conversation.id, { title }), "sidebar.renameFailed")) setRenaming(null);
  };

  return <div className={`${className} kaus-row-with-menu`} onContextMenu={onContextMenu}>
    {renaming !== null
      ? <input className="kaus-sidebar-title-input" autoFocus aria-label={t("sidebar.renameField")} value={renaming} disabled={busy}
          onChange={event => setRenaming(event.target.value)} onFocus={event => event.currentTarget.select()}
          onBlur={() => { if (!busy) setRenaming(null); }}
          onKeyDown={event => {
            if (event.nativeEvent.isComposing) return;
            if (draftEscape(event, renaming !== conversation.title, () => setRenaming(null))) return;
            if (event.key === "Enter") { event.preventDefault(); void rename(); }
          }} />
      : <button type="button" className="kaus-row-open" onClick={onOpen}>{children}</button>}
    <ConversationRowMenu title={conversation.title} pinned={pinned} canArchive={!running} busy={busy} point={point}
      onPin={() => { const next = { ...pins, [project]: pinned ? (pins[project] ?? []).filter(id => id !== conversation.id) : [...(pins[project] ?? []), conversation.id] }; setPins(next); saveSidebarPins(next); }}
      onRename={() => setRenaming(conversation.title)}
      onContinue={onOpenConversation ? () => setContinuing(true) : undefined}
      onArchive={() => { void work(() => archiveConversation(conversation.id), "sidebar.archiveFailed").then(ok => {
        if (!ok) return;
        const wasPinned = pinned;
        unpin();
        // Archiving keeps everything: 「撤销」 brings the conversation (and its pin) back.
        undoToast(t("ui.archived", { name: conversation.title }), async () => {
          try {
            await archiveConversation(conversation.id, false);
            if (wasPinned) { const current = readSidebarPins(); saveSidebarPins({ ...current, [project]: [...(current[project] ?? []).filter(id => id !== conversation.id), conversation.id] }); }
            onChanged();
          } catch { toast(t("ui.undoFailed"), "bad"); }
        });
      }); }}
      onDelete={() => { void (async () => {
        if (!await confirmAsync(t("conversation.row.deleteConfirm"), { title: t("conversation.row.deleteTitle", { title: conversation.title }), okLabel: t("conversation.row.delete"), danger: true })) return;
        if (await work(() => deleteConversation(conversation.id), "sidebar.deleteFailed")) unpin();
      })(); }} />
    {continuing && onOpenConversation && <ContinuationDialog source={{ kind: "conversation", id: conversation.id, projectId: conversation.projectId }}
      onClose={() => setContinuing(false)} onCreated={id => { setContinuing(false); onOpenConversation(id); }} />}
  </div>;
}

/** A native history row (sidebar and project page): open it, or from ⋯ / right-click continue with another Agent or remove it. */
export function HistoryRow({ item, projectId, className, onOpen, onOpenConversation, children }: {
  item: ArchiveItem;
  projectId: string;
  className: string;
  onOpen: () => void;
  onOpenConversation: (conversationId: string) => void;
  children: ReactNode;
}) {
  const { t } = useLocale();
  const { point, onContextMenu } = usePoint();
  const [busy, setBusy] = useState(false);
  const [continuing, setContinuing] = useState(false);
  const remove = async () => {
    setBusy(true);
    try {
      await projectHistoryApi.remove(item.id, projectId); notifyHistoryChanged();
      // The archive stays owned; 「撤销」 links it back into the project.
      undoToast(t("ui.removedFromProject", { name: item.title }), async () => {
        try { await projectHistoryApi.link(item.id, projectId); notifyHistoryChanged(); } catch { toast(t("project.history.removeFailed"), "bad"); }
      });
    }
    catch { toast(t("project.history.removeFailed"), "bad"); }
    finally { setBusy(false); }
  };
  // 2026-11-18「原生历史导入侧栏」: opening a record makes it an ordinary conversation when its engine is
  // connected; otherwise it stays read-only.
  const open = async () => {
    setBusy(true);
    try {
      const conversationId = await projectHistoryApi.adopt(item.id, projectId);
      if (conversationId === null) { onOpen(); return; }
      notifyHistoryChanged(); window.dispatchEvent(new Event("kaus:conversations-changed"));
      onOpenConversation(conversationId);
    }
    catch { onOpen(); }
    finally { setBusy(false); }
  };
  return <div className={`${className} kaus-row-with-menu`} onContextMenu={onContextMenu}>
    <button type="button" className="kaus-row-open" title={item.title} disabled={busy} onClick={() => void open()}>{children}</button>
    <RowMenu busy={busy} point={point} triggerLabel={t("conversation.menuFor", { title: item.title })} menuLabel={t("conversation.menu")} items={[
      { id: "continue", icon: <ArrowRightLeft size={16} aria-hidden />, label: t("project.move.continue"), onSelect: () => setContinuing(true) },
      { id: "remove", icon: <Unlink size={16} aria-hidden />, label: t("project.history.remove"), destructive: true, onSelect: () => void remove() },
    ]} />
    {continuing && <ContinuationDialog source={{ kind: "archive", id: item.id, projectId }} onClose={() => setContinuing(false)}
      onCreated={id => { setContinuing(false); onOpenConversation(id); }} />}
  </div>;
}
