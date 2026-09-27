import { useEffect, useRef, useState, useSyncExternalStore, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { RotateCw, X } from "lucide-react";

import { t } from "../i18n";

/* ------------------------------------------------------------------ *
 * Shared UI primitives — platinum × gold, Hermes-style detail.
 * Overlay/Card/FieldRow/Pill/Button/Modal + imperative toast & confirm.
 * ------------------------------------------------------------------ */

/* Theme helpers (live here, not in the HTTP layer): cream() builds an
   ink-at-opacity color-mix string; dname() picks an agent's display name. */
export const cream = (pct: number) => `color-mix(in srgb, var(--midground-base) ${pct}%, transparent)`;
export const dname = (n: { name: string; label?: string }) => n.label || n.name;

export const HAIR = `1px solid ${cream(11)}`;

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block animate-spin rounded-full ${className}`}
      style={{ width: 14, height: 14, border: `2px solid ${cream(20)}`, borderTopColor: "var(--gold)" }}
    />
  );
}

/* Shared async-fetch states (spinner-while-loading / inline error line). */
export const Loading = () => <div className="flex items-center gap-2 py-1 text-xs" style={{ color: cream(45) }}><Spinner /> {t("ui.loading")}</div>;
export const ErrLine = ({ e }: { e: string }) => <div className="py-1 text-xs" style={{ color: "var(--danger)" }}>{t("ui.loadFailed", { error: e })}</div>;

/* Overlay-scale async wrapper: shows a text-sm error line, then a text-sm
   spinner while `data` is null, else renders children with the loaded value.
   Collapses the duplicated `err ? … : data === null ? <Spinner/>加载中… : (…)`
   ternary that every full-screen overlay hand-rolls. */
export function AsyncState<T>({ data, err, children }: { data: T | null; err: string | null; children: (data: T) => ReactNode }) {
  if (err) return <div className="text-sm" style={{ color: "var(--danger)" }}>{t("ui.loadFailed", { error: err })}</div>;
  if (data === null) return <div className="flex items-center gap-2 text-sm" style={{ color: cream(50) }}><Spinner /> {t("ui.loading")}</div>;
  return <>{children(data)}</>;
}

type Tone = "default" | "gold" | "success" | "danger" | "muted";
const PILL_TONE: Record<Tone, CSSProperties> = {
  default: { background: cream(7), color: cream(70), border: `1px solid ${cream(14)}` },
  gold: { background: "var(--gold-soft)", color: "var(--accent-text)", border: `1px solid color-mix(in srgb, var(--gold) 35%, transparent)` },
  success: { background: "rgba(122,176,122,.14)", color: "#5c7d52", border: "1px solid rgba(122,176,122,.34)" },
  danger: { background: "rgba(179,87,63,.12)", color: "var(--danger)", border: "1px solid rgba(179,87,63,.32)" },
  muted: { background: "transparent", color: cream(45), border: `1px solid ${cream(13)}` },
};

export function Pill({ tone = "default", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="inline-flex items-center rounded-[1px] px-1.5 py-0.5 text-[0.6rem] uppercase tracking-[0.14em]"
      style={PILL_TONE[tone]}
    >
      {children}
    </span>
  );
}

type BtnVariant = "primary" | "ghost" | "danger";
export function Button({
  variant = "ghost",
  children,
  onClick,
  disabled,
  title,
  className = "",
  "aria-pressed": ariaPressed,
}: {
  variant?: BtnVariant;
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
  className?: string;
  "aria-pressed"?: boolean;
}) {
  const base: CSSProperties =
    variant === "primary"
      ? { background: "var(--gold-soft)", color: "var(--accent-text)", border: `1px solid color-mix(in srgb, var(--gold) 40%, transparent)` }
      : variant === "danger"
        ? { background: "transparent", color: "var(--danger)", border: `1px solid color-mix(in srgb, var(--danger) 30%, transparent)` }
        : { background: "transparent", color: cream(70), border: HAIR };
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-pressed={ariaPressed}
      className={`whitespace-nowrap rounded-md px-3 py-1.5 text-xs transition-colors disabled:opacity-40 ${className}`}
      style={base}
    >
      {children}
    </button>
  );
}

/* Full-screen module panel (ports the vanilla .netwrap overlays). */
export function Overlay({
  title,
  onClose,
  onReload,
  actions,
  children,
}: {
  title: string;
  onClose: () => void;
  onReload?: () => void | Promise<unknown>;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const [reloading, setReloading] = useState(false);
  const runReload = () => {
    if (!onReload || reloading) return;
    setReloading(true);
    const minDelay = new Promise((resolve) => window.setTimeout(resolve, 450));
    Promise.allSettled([Promise.resolve(onReload()), minDelay]).finally(() => setReloading(false));
  };

  return (
    <div className="flex h-full flex-col" style={{ color: "var(--midground-base)" }}>
      <div className="flex items-center gap-2.5 px-7 pt-5 pb-3">
        <span className="gold-text text-xl font-bold uppercase tracking-[0.16em]">{title}</span>
        <span className="ml-auto flex items-center gap-3">
          {actions}
          {onReload && (
            <button
              type="button"
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                runReload();
              }}
              disabled={reloading}
              /* 第 8 件：浮层页头这两句原来是硬编码英文（走查 F9）。 */
              title={reloading ? t("ui.refreshing") : t("ui.refresh")}
              aria-label={reloading ? t("ui.refreshing") : t("ui.refresh")}
              className="grid size-7 cursor-pointer place-items-center rounded hover:bg-black/[0.05] disabled:cursor-wait disabled:opacity-70"
              style={{ color: reloading ? "var(--accent-text)" : cream(50) }}
            >
              <RotateCw className={`size-4 ${reloading ? "animate-spin" : ""}`} />
            </button>
          )}
          {/* AD-98：全站没有「上一级」，所以没有返回箭头；浮层是**弹窗**，允许关闭。
              关闭钮因此挪到右上角并换成 ×（Esc 仍然有效，由外壳统一处理）。 */}
          <button
            type="button"
            onClick={onClose}
            title={`${t("common.close")} (Esc)`}
            aria-label={t("common.close")}
            data-testid="overlay-close"
            className="grid size-7 shrink-0 place-items-center rounded hover:bg-black/[0.05]"
            style={{ color: cream(55) }}
          >
            <X className="size-5" />
          </button>
        </span>
      </div>
      <div className="gold-bar mx-7 h-px opacity-60" />
      <div className="min-h-0 flex-1 overflow-y-auto px-7 py-6">{children}</div>
    </div>
  );
}

/* Hermes-style card: icon + uppercase title header, hairline border. */
export function Card({
  title,
  icon: Icon,
  right,
  children,
  className = "",
}: {
  title?: string;
  icon?: React.ComponentType<{ className?: string; style?: CSSProperties }>;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`overflow-hidden ${className}`} style={{ background: cream(2), border: `1px solid ${cream(16)}` }}>
      {title && (
        <div className="flex items-center gap-2.5 px-4 py-3" style={{ borderBottom: `1px solid ${cream(16)}` }}>
          {Icon && <Icon className="size-[18px] shrink-0" style={{ color: "var(--accent-text)" }} />}
          <span className="text-[0.82rem] font-bold uppercase tracking-[0.16em]">{title}</span>
          {right && <span className="ml-auto">{right}</span>}
        </div>
      )}
      <div className="p-4">{children}</div>
    </div>
  );
}

/* Detail-drawer section: small uppercase label + body. */
export function Section({ title, meta, children }: { title: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <div className="py-4" style={{ borderTop: `1px solid ${cream(13)}` }}>
      <div className="mb-3 flex items-center gap-2">
        <span className="gold-bar inline-block shrink-0" style={{ width: 3, height: 13, borderRadius: 1 }} />
        <h3 className="text-[0.78rem] font-bold uppercase tracking-[0.16em]" style={{ color: "var(--midground-base)" }}>
          {title}
        </h3>
        {meta && <span className="text-[0.65rem]" style={{ color: cream(45) }}>{meta}</span>}
      </div>
      {children}
    </div>
  );
}

export function FieldRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 py-1.5 text-xs">
      <div className="w-20 shrink-0" style={{ color: cream(45) }}>{label}</div>
      <div className="min-w-0 flex-1" style={{ color: "var(--midground-base)" }}>{children}</div>
    </div>
  );
}

/* Centered modal (declarative: render when open).
   z-[70]:必须压过 Kanban 任务详情抽屉(z-[61]),否则从抽屉里弹的确认框会被盖住、按钮看似失灵。
   Esc 只由「最顶层」modal 消费并 stopImmediatePropagation——防止一次 Esc 级联关掉底下的 overlay/抽屉。 */
const _modalStack: symbol[] = [];
export function Modal({ onClose, children, width = 520 }: { onClose: () => void; children: ReactNode; width?: number }) {
  const contentRef = useRef<HTMLDivElement | null>(null);
  const idRef = useRef(Symbol("modal"));
  useEffect(() => {
    _modalStack.push(idRef.current);
    return () => {
      const i = _modalStack.indexOf(idRef.current);
      if (i >= 0) _modalStack.splice(i, 1);
    };
  }, []);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (_modalStack[_modalStack.length - 1] !== idRef.current) return;
      if (contentRef.current?.querySelector('.kaus-bar-menu > button[aria-expanded="true"]')) return;
      event.stopImmediatePropagation();
      onClose();
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-[70] grid place-items-center p-4"
      style={{ background: "var(--overlay-scrim)" }}
      onClick={onClose}
    >
      <div
        ref={contentRef}
        className="max-h-[88vh] overflow-y-auto rounded-xl p-5"
        style={{ width, maxWidth: "94vw", background: "var(--panel)", border: HAIR, boxShadow: "var(--shadow-lg)" }}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}

export function ModalTitle({ children }: { children: ReactNode }) {
  return <h3 className="mb-1 text-sm font-semibold uppercase tracking-[0.12em]">{children}</h3>;
}
export function ModalSub({ children }: { children: ReactNode }) {
  return <p className="mb-3 text-xs leading-relaxed" style={{ color: cream(55) }}>{children}</p>;
}
export function inputStyle(): CSSProperties {
  return { width: "100%", padding: "7px 9px", borderRadius: 8, border: `1px solid ${cream(18)}`, background: "var(--surface-raised)", color: "var(--midground-base)", font: "12px var(--theme-font-mono, monospace)", outline: "none" };
}

/* ---------------- imperative toast ---------------- */
type ToastItem = { id: number; msg: string; tone: "ok" | "bad" | "" };
let _toasts: ToastItem[] = [];
const _toastSubs = new Set<() => void>();
function _emitToast() { for (const f of _toastSubs) f(); }
export function toast(msg: string, tone: "ok" | "bad" | "" = "") {
  const id = Date.now() + Math.random();
  _toasts = [..._toasts, { id, msg, tone }];
  _emitToast();
  setTimeout(() => { _toasts = _toasts.filter((t) => t.id !== id); _emitToast(); }, 3600);
}
export function ToastHost() {
  const items = useSyncExternalStore(
    (cb) => { _toastSubs.add(cb); return () => _toastSubs.delete(cb); },
    () => _toasts,
    () => _toasts,
  );
  return createPortal(
    /* z-[80]:toast 是最高层反馈,必须压过一切抽屉/弹窗(Kanban 详情 z-61、Modal z-70),否则操作提示被盖住看不见 */
    <div className="fixed bottom-5 right-5 z-[80] flex flex-col gap-2">
      {items.map((t) => (
        <div
          key={t.id}
          className="rounded-lg px-3.5 py-2 text-xs shadow-lg"
          style={{
            background: "var(--panel)",
            border: `1px solid ${t.tone === "bad" ? "color-mix(in srgb, var(--danger) 40%, transparent)" : cream(16)}`,
            color: t.tone === "bad" ? "var(--danger)" : "var(--midground-base)",
            boxShadow: "0 10px 30px rgba(60,50,20,.16)",
          }}
        >
          {t.msg}
        </div>
      ))}
    </div>,
    document.body,
  );
}

/* ---------------- imperative confirm ---------------- */
type ConfirmState = { msg: string; okLabel: string; danger: boolean; resolve: (v: boolean) => void } | null;
let _confirm: ConfirmState = null;
const _confirmSubs = new Set<() => void>();
function _emitConfirm() { for (const f of _confirmSubs) f(); }
export function confirmAsync(msg: string, opts: { okLabel?: string; danger?: boolean } = {}): Promise<boolean> {
  return new Promise((resolve) => {
    if (_confirm) _confirm.resolve(false);   // 单槽:新确认顶掉旧的时,旧 promise 以"取消"结算,调用方不会永久挂起
    _confirm = { msg, okLabel: opts.okLabel ?? t("ui.ok"), danger: !!opts.danger, resolve };
    _emitConfirm();
  });
}
export function ConfirmHost() {
  const s = useSyncExternalStore(
    (cb) => { _confirmSubs.add(cb); return () => _confirmSubs.delete(cb); },
    () => _confirm,
    () => _confirm,
  );
  if (!s) return null;
  const done = (v: boolean) => { s.resolve(v); _confirm = null; _emitConfirm(); };
  return (
    <Modal onClose={() => done(false)} width={420}>
      <p className="mb-4 text-sm leading-relaxed" style={{ color: "var(--midground-base)" }}>{s.msg}</p>
      <div className="flex justify-end gap-3">
        <Button onClick={() => done(false)}>{t("ui.cancel")}</Button>
        <Button variant={s.danger ? "danger" : "primary"} onClick={() => done(true)}>{s.okLabel}</Button>
      </div>
    </Modal>
  );
}
