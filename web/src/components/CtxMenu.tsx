import { ErrorNotice, toast, undoToast } from "./ui";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Copy, FolderInput, MessageSquarePlus, Pencil, Plus, Trash2 } from "lucide-react";
import { useLocale } from "../i18n";
import { ADD_EXISTING_CONVERSATIONS } from "../lib/projectHistory";
import { deleteAgent, deletePreview, forkAgent, renameAgent, restoreDeletedAgent, runningConversationsOf, type OrgNode } from "../lib/api";
import { archiveConversation, describeFailure, fetchRecentConversations } from "../lib/sessionApi";
import { cream, dname } from "./ui";
import { moveProjectWithUndo } from "../lib/projectMove";

const Field = ({ children }: { children: React.ReactNode }) => <div className="p-2.5">{children}</div>;

type Mode = "menu" | "rename" | "fork" | "move" | "delete";

/** Sub-project names shown in the delete confirm before truncating. */
const NAMED_DESCENDANTS = 3;

// Project ⋯ menu (wheel nodes and the project page header) — add existing conversations / rename / fork / delete.
// Positioned at (x, y) = panel top-left. A transparent backdrop closes it.
export function CtxMenu({
  node,
  x,
  y,
  onClose,
  onChanged,
  onDeleted,
  nodes,
  onAddChild,
}: {
  node: OrgNode;
  x: number;
  y: number;
  onClose: () => void;
  onChanged: () => void;
  /** Called after a confirmed delete, e.g. to leave the deleted Project's page. */
  onDeleted?: (name: string) => void;
  /** All projects, for 「移到」 (2026-11-18). */
  nodes?: Record<string, OrgNode>;
  /** 「新建子项目」 under this project. */
  onAddChild?: (parent: string) => void;
}) {
  const { t } = useLocale();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const [mode, setMode] = useState<Mode>("menu");
  const [renameVal, setRenameVal] = useState(dname(node));
  const [forkId, setForkId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  /** Running conversations reported by a refused delete; a confirmed retry stops them first. */
  const [running, setRunning] = useState<number | null>(null);
  /** Sub-projects deleted together with this one; `null` until the preview loads. */
  const [descendants, setDescendants] = useState<string[] | null>(null);
  const isDefault = node.name === "default";
  useEffect(() => {
    if (mode !== "delete") return;
    let cancelled = false;
    setDescendants(null);
    deletePreview(node.name)
      .then(preview => { if (!cancelled) setDescendants(preview.descendants.map(item => item.label || item.name)); })
      .catch(e => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [mode, node.name]);
  useLayoutEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const close = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeRef.current(); }
    };
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("keydown", close); if (opener?.isConnected) opener.focus({ preventScroll: true }); };
  }, []);
  useLayoutEffect(() => {
    const element = panelRef.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    element.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`;
    element.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`;
    element.querySelector<HTMLElement>("input, button:not(:disabled)")?.focus();
  }, [mode, x, y]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      onChanged();
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };
  /* 删除项目（2026-11-18「删除与撤销」）：项目、子项目可原位恢复，对话只是归档，所以直接删除并给「撤销」；
     只有要先停止运行中的对话时才确认（停止无法撤回）。撤销按由上到下恢复项目，再把这次归档的对话放回。 */
  const runDelete = async (force = false) => {
    setBusy(true);
    setErr(null);
    try {
      const preview = await deletePreview(node.name);
      const subtree = [node.name, ...preview.descendants.map(item => item.name)];
      const lists = await Promise.all(subtree.map(slug => fetchRecentConversations(undefined, { project: `project:${slug}`, limit: 200 })
        .then(result => result.conversations.map(item => item.id)).catch(() => [] as string[])));
      const conversations = lists.flat();
      const result = await deleteAgent(node.name, { force });
      onChanged();
      onDeleted?.(node.name);
      onClose();
      const count = result.cascade?.length ?? 0;
      const done = count ? t("project.delete.done", { name: dname(node), count }) : t("ui.deleted", { name: dname(node) });
      // Confirm or undo, never both (2026-11-18): after confirming 停止并删除 there is only a notice.
      if (force) { toast(done, "ok"); return; }
      undoToast(done, async () => {
        try {
          for (const slug of [node.name, ...[...(result.cascade ?? [])].reverse()]) await restoreDeletedAgent(slug);
          await Promise.all(conversations.map(id => archiveConversation(id, false).catch(() => undefined)));
          onChanged();
          window.dispatchEvent(new Event("kaus:conversations-changed"));
        } catch (failure) { toast(describeFailure(failure), "bad"); }
      });
    } catch (e) {
      const ids = runningConversationsOf(e);
      if (ids && !force) { setRunning(ids.length); setMode("delete"); }
      else setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  const panel: React.CSSProperties = {
    left: x,
    top: y,
    background: "var(--surface-3)",
    border: 0,
    color: "var(--midground-base)",
    boxShadow: "var(--shadow-popover)",
  };
  const item = "flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-[var(--control-hover)]";
  const inputStyle: React.CSSProperties = {
    background: cream(8),
    border: `1px solid ${cream(16)}`,
    color: "var(--midground-base)",
  };
  const Actions = ({ onOk, okLabel, danger, disabled }: { onOk: () => void; okLabel: string; danger?: boolean; disabled?: boolean }) => (
    <div className="mt-2 flex items-center justify-end gap-3 text-xs">
      <button onClick={() => { setErr(null); setRunning(null); setMode("menu"); }} style={{ color: cream(55) }}>
        {t("common.cancel")}
      </button>
      <button disabled={busy || disabled} onClick={onOk} style={{ color: danger ? "var(--danger)" : "var(--accent-text)" }}>
        {okLabel}
      </button>
    </div>
  );
  const runFork = () => {
    const nextId = forkId.trim();
    if (!nextId) {
      setErr(t("ctx.forkIdRequired"));
      return;
    }
    run(() => forkAgent(node.name, nextId));
  };

  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} />
      <div ref={panelRef} className="kaus-context-menu fixed z-50 w-48 overflow-hidden rounded-md" style={panel} role={mode === "menu" ? "menu" : "dialog"} aria-label={dname(node)} onClick={(e) => e.stopPropagation()} onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
        if (event.key === "Tab" && mode === "menu") { closeRef.current(); return; }
        if (mode !== "menu") return;
        const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'));
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : event.key === "ArrowDown" ? (index + 1) % items.length : event.key === "ArrowUp" ? (index - 1 + items.length) % items.length : -1;
        if (next >= 0) { event.preventDefault(); items[next]?.focus(); }
      }}>
        {mode === "menu" && (
          <>
            <button role="menuitem" className={item} onClick={() => {
              window.dispatchEvent(new CustomEvent(ADD_EXISTING_CONVERSATIONS, { detail: { projectRef: node.name } }));
              onClose();
            }}><MessageSquarePlus className="size-3.5" />{t("project.history.add")}</button>
            {onAddChild && <button role="menuitem" disabled={busy} className={item} onClick={() => { onAddChild(node.name); onClose(); }}>
              <Plus className="size-3.5" />{t("ctx.addChild")}
            </button>}
            <button role="menuitem" disabled={busy} className={item} onClick={() => setMode("rename")}>
              <Pencil className="size-3.5" />
              {t("ctx.rename")}
            </button>
            <button role="menuitem" disabled={busy} className={item} onClick={() => setMode("fork")}>
              <Copy className="size-3.5" />
              {t("ctx.fork")}
            </button>
            {!isDefault && nodes && <button role="menuitem" disabled={busy} className={item} onClick={() => setMode("move")}>
              <FolderInput className="size-3.5" />{t("ctx.move")}
            </button>}
            {!isDefault && (
              <button role="menuitem" disabled={busy} className={item} style={{ color: "var(--danger)" }} onClick={() => void runDelete()}>
                <Trash2 className="size-3.5" />
                {t("ctx.delete")}
              </button>
            )}
          </>
        )}

        {mode === "move" && nodes && (
          <div role="group" aria-label={t("ctx.move")} className="max-h-72 overflow-auto py-1">
            {moveTargets(nodes, node.name).map(({ id, depth }) => (
              <button key={id} className={item} style={{ paddingLeft: 12 + depth * 12 }} disabled={busy} onClick={() => {
                const target = nodes[id];
                onClose();
                void moveProjectWithUndo({ node: node.name, from: node.parent ?? null, to: id, onChanged,
                  message: t("project.moved", { name: dname(node), target: dname(target) }) });
              }}>{dname(nodes[id])}</button>
            ))}
          </div>
        )}

        {mode === "rename" && (
          <Field>
            <div className="mb-1 text-[0.6875rem]" style={{ color: cream(55) }}>
              {t("ctx.displayName")}
            </div>
            <input
              autoFocus
              value={renameVal}
              onChange={(e) => setRenameVal(e.target.value)}
              className="w-full rounded px-2 py-1 text-xs outline-none"
              style={inputStyle}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing && e.nativeEvent.keyCode !== 229) run(() => renameAgent(node.name, renameVal.trim())); }}
            />
            {err && <ErrorNotice error={err} className="mt-1 text-[0.6875rem]" />}
            <Actions onOk={() => run(() => renameAgent(node.name, renameVal.trim()))} okLabel={t("common.save")} />
          </Field>
        )}

        {mode === "fork" && (
          <Field>
            <div className="mb-1 text-[0.6875rem]" style={{ color: cream(55) }}>
              {t("ctx.forkId")}
            </div>
            <input
              autoFocus
              value={forkId}
              onChange={(e) => setForkId(e.target.value)}
              placeholder={t("ctx.forkIdExample")}
              className="w-full rounded px-2 py-1 text-xs outline-none"
              style={inputStyle}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing && e.nativeEvent.keyCode !== 229) runFork(); }}
            />
            {err && <ErrorNotice error={err} className="mt-1 text-[0.6875rem]" />}
            <Actions onOk={runFork} okLabel={t("ctx.fork")} />
          </Field>
        )}

        {mode === "delete" && (
          <Field>
            <div className="mb-1 text-[0.6875rem] leading-relaxed" style={{ color: cream(72) }}>
              {descendants && descendants.length > 0
                ? t("project.delete.cascadeConfirm", {
                    name: dname(node),
                    count: descendants.length,
                    names: descendants.slice(0, NAMED_DESCENDANTS).join(t("common.listSeparator"))
                      + (descendants.length > NAMED_DESCENDANTS ? "…" : ""),
                  })
                : t("project.delete.confirm", { name: dname(node) })}
            </div>
            {running !== null && <div role="alert" className="mb-1 text-[0.6875rem] leading-relaxed" style={{ color: "var(--danger)" }}>
              {t("project.delete.stopConfirm", { count: running })}
            </div>}
            {err && <ErrorNotice error={err} className="mt-1 text-[0.6875rem]" />}
            <Actions onOk={() => void runDelete(true)} okLabel={t("project.delete.stopAndDelete")} disabled={descendants === null} danger />
          </Field>
        )}
      </div>
    </>
  );
}

/** Projects a node can move under: not itself, not its descendants, not its current parent. Tree order with depth. */
export function moveTargets(nodes: Record<string, OrgNode>, name: string): { id: string; depth: number }[] {
  const out: { id: string; depth: number }[] = [];
  const parent = nodes[name]?.parent ?? null;
  const walk = (id: string, depth: number) => {
    if (id === name || !nodes[id]) return;
    if (id !== parent) out.push({ id, depth });
    for (const child of [...(nodes[id].children || [])].sort((a, b) => dname(nodes[a] ?? { name: a }).localeCompare(dname(nodes[b] ?? { name: b })))) walk(child, depth + 1);
  };
  const roots = Object.keys(nodes).filter(id => !nodes[id].parent || !nodes[nodes[id].parent!]);
  for (const root of roots.sort()) walk(root, 0);
  return out;
}
