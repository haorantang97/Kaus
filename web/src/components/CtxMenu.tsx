import { ErrorNotice } from "./ui";
import { useLayoutEffect, useRef, useState } from "react";
import { Copy, MessageSquarePlus, Pencil, Pin, PinOff, Trash2 } from "lucide-react";
import { useLocale } from "../i18n";
import { ADD_EXISTING_CONVERSATIONS } from "../lib/projectHistory";
import { deleteAgent, forkAgent, renameAgent, setPin, type OrgNode } from "../lib/api";
import { cream, dname } from "./ui";

const Field = ({ children }: { children: React.ReactNode }) => <div className="p-2.5">{children}</div>;

type Mode = "menu" | "rename" | "fork" | "delete";

// Sidebar ⋯ menu — pin / rename / fork / delete, wired to the backend.
// Positioned at (x, y) = panel top-left. A transparent backdrop closes it.
export function CtxMenu({
  node,
  x,
  y,
  onClose,
  onChanged,
}: {
  node: OrgNode;
  x: number;
  y: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { locale } = useLocale();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const [mode, setMode] = useState<Mode>("menu");
  const [renameVal, setRenameVal] = useState(dname(node));
  const [forkId, setForkId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const isDefault = node.name === "default";
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

  const panel: React.CSSProperties = {
    left: x,
    top: y,
    background: "var(--surface-3)",
    border: 0,
    color: "var(--midground-base)",
    boxShadow: "var(--shadow-popover)",
  };
  const item = "flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-black/[0.05]";
  const inputStyle: React.CSSProperties = {
    background: cream(8),
    border: `1px solid ${cream(16)}`,
    color: "var(--midground-base)",
  };
  const Actions = ({ onOk, okLabel, danger }: { onOk: () => void; okLabel: string; danger?: boolean }) => (
    <div className="mt-2 flex items-center justify-end gap-3 text-xs">
      <button onClick={() => { setErr(null); setMode("menu"); }} style={{ color: cream(55) }}>
        取消
      </button>
      <button disabled={busy} onClick={onOk} style={{ color: danger ? "var(--danger)" : "var(--accent-text)" }}>
        {okLabel}
      </button>
    </div>
  );
  const runFork = () => {
    const nextId = forkId.trim();
    if (!nextId) {
      setErr("新 ID 不能为空");
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
            }}><MessageSquarePlus className="size-3.5" />{locale === "zh" ? "添加已有对话" : "Add existing conversations"}</button>
            <button role="menuitem" disabled={busy} className={item} onClick={() => run(() => setPin(node.name, !node.is_pinned))}>
              {node.is_pinned ? <PinOff className="size-3.5" /> : <Pin className="size-3.5" />}
              {node.is_pinned ? "取消置顶" : "置顶"}
            </button>
            <button role="menuitem" disabled={busy} className={item} onClick={() => setMode("rename")}>
              <Pencil className="size-3.5" />
              改名
            </button>
            <button role="menuitem" disabled={busy} className={item} onClick={() => setMode("fork")}>
              <Copy className="size-3.5" />
              复制
            </button>
            {!isDefault && (
              <button role="menuitem" disabled={busy} className={item} style={{ color: "var(--danger)" }} onClick={() => setMode("delete")}>
                <Trash2 className="size-3.5" />
                删除
              </button>
            )}
          </>
        )}

        {mode === "rename" && (
          <Field>
            <div className="mb-1 text-[0.7rem]" style={{ color: cream(55) }}>
              新显示名
            </div>
            <input
              autoFocus
              value={renameVal}
              onChange={(e) => setRenameVal(e.target.value)}
              className="w-full rounded px-2 py-1 text-xs outline-none"
              style={inputStyle}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing && e.nativeEvent.keyCode !== 229) run(() => renameAgent(node.name, renameVal.trim())); }}
            />
            {err && <ErrorNotice error={err} className="mt-1 text-[0.7rem]" />}
            <Actions onOk={() => run(() => renameAgent(node.name, renameVal.trim()))} okLabel="保存" />
          </Field>
        )}

        {mode === "fork" && (
          <Field>
            <div className="mb-1 text-[0.7rem]" style={{ color: cream(55) }}>
              复制为新 ID（小写、连字符）
            </div>
            <input
              autoFocus
              value={forkId}
              onChange={(e) => setForkId(e.target.value)}
              placeholder="如 trading-2"
              className="w-full rounded px-2 py-1 text-xs outline-none"
              style={inputStyle}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing && e.nativeEvent.keyCode !== 229) runFork(); }}
            />
            {err && <ErrorNotice error={err} className="mt-1 text-[0.7rem]" />}
            <Actions onOk={runFork} okLabel="复制" />
          </Field>
        )}

        {mode === "delete" && (
          <Field>
            <div className="mb-1 text-[0.72rem] leading-relaxed" style={{ color: cream(72) }}>
              删除「{dname(node)}」？归档到 trash，可恢复。
            </div>
            {err && <ErrorNotice error={err} className="mt-1 text-[0.7rem]" />}
            <Actions onOk={() => run(() => deleteAgent(node.name))} okLabel="删除" danger />
          </Field>
        )}
      </div>
    </>
  );
}
