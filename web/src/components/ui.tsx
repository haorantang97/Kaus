import { useRef, useState, useSyncExternalStore, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { RotateCw, X } from "lucide-react";

import { t, useLocale } from "../i18n";
import { failureDiagnostic, failureSummary } from "../lib/userFacingErrors";
import { useModalLayer } from "../hooks/useModalLayer";

/* ------------------------------------------------------------------ *
 * Shared UI primitives — quiet controls and clear module hierarchy.
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
export function ErrorNotice({ error, className = "" }: { error: unknown; className?: string }) {
  const { locale } = useLocale();
  const summary = failureSummary(error);
  const diagnostic = failureDiagnostic(error);
  return <div role="alert" className={className} style={{ color: "var(--danger)" }}>
    <span>{summary}</span>
    {diagnostic && diagnostic !== summary && <details className="mt-1">
      <summary className="cursor-pointer text-xs" style={{ color: cream(55) }}>{locale === "zh" ? "详情" : "Details"}</summary>
      <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-all text-xs" style={{ color: cream(60) }}>{diagnostic}</pre>
    </details>}
  </div>;
}
export const ErrLine = ({ e }: { e: string }) => <ErrorNotice error={e} className="py-1 text-xs" />;

/* Overlay-scale async wrapper: shows a text-sm error line, then a text-sm
   spinner while `data` is null, else renders children with the loaded value.
   Collapses the duplicated `err ? … : data === null ? <Spinner/>加载中… : (…)`
   ternary that every full-screen overlay hand-rolls. */
export function AsyncState<T>({ data, err, children }: { data: T | null; err: string | null; children: (data: T) => ReactNode }) {
  if (err) return <ErrorNotice error={err} className="text-sm" />;
  if (data === null) return <div className="flex items-center gap-2 text-sm" style={{ color: cream(50) }}><Spinner /> {t("ui.loading")}</div>;
  return <>{children(data)}</>;
}

type Tone = "default" | "gold" | "success" | "danger" | "muted";
const PILL_TONE: Record<Tone, CSSProperties> = {
  default: { background: "transparent", color: cream(70) },
  gold: { background: "var(--gold-soft)", color: "var(--accent-text)" },
  success: { background: "rgba(122,176,122,.12)", color: "#5c7d52" },
  danger: { background: "color-mix(in srgb, var(--danger) 9%, transparent)", color: "var(--danger)" },
  muted: { background: "transparent", color: cream(50) },
};

export function Pill({ tone = "default", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="kaus-pill"
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
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-pressed={ariaPressed}
      className={`kaus-button kaus-button--${variant} ${className}`}
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
    <div className="kaus-overlay">
      <div className="kaus-overlay-header">
        <h2 className="kaus-overlay-title">{title}</h2>
        <span className="kaus-overlay-actions">
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
              aria-busy={reloading}
              /* 第 8 件：浮层页头这两句原来是硬编码英文（走查 F9）。 */
              title={reloading ? t("ui.refreshing") : t("ui.refresh")}
              aria-label={reloading ? t("ui.refreshing") : t("ui.refresh")}
              className="kaus-icon-control"
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
            className="kaus-icon-control"
            style={{ color: cream(55) }}
          >
            <X className="size-5" />
          </button>
        </span>
      </div>
      <div className="kaus-overlay-body">{children}</div>
    </div>
  );
}

/* Grouped content with a quiet surface and a readable heading. */
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
    <div className={`kaus-card ${className}`}>
      {title && (
        <div className="kaus-card-header">
          {Icon && <Icon className="size-[18px] shrink-0" />}
          <h3 className="kaus-card-title">{title}</h3>
          {right && <span className="ml-auto">{right}</span>}
        </div>
      )}
      <div className="kaus-card-body">{children}</div>
    </div>
  );
}

/* Detail-drawer sections are grouped by whitespace and heading weight. */
export function Section({ title, meta, children }: { title: string; meta?: ReactNode; children: ReactNode }) {
  return (
    <div className="kaus-section">
      <div className="kaus-section-header">
        <h3 className="kaus-section-title">
          {title}
        </h3>
        {meta && <span className="kaus-section-meta">{meta}</span>}
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
export function Modal({ onClose, children, width = 520 }: { onClose: () => void; children: ReactNode; width?: number }) {
  const { rootRef, contentRef } = useModalLayer(onClose);
  const backdropDown = useRef(true);

  return createPortal(
    <div
      ref={rootRef}
      className="kaus-modal-layer fixed inset-0 z-[70] grid place-items-center p-4"
      data-kaus-modal="true"
      style={{ background: "var(--overlay-scrim)" }}
      onPointerDown={event => { backdropDown.current = event.target === event.currentTarget; }}
      onClick={event => { if (event.target === event.currentTarget && backdropDown.current) onClose(); }}
    >
      <div
        ref={contentRef}
        className="kaus-modal-content overflow-y-auto p-5"
        tabIndex={-1}
        style={{ width, maxWidth: "94vw" }}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}

export function ModalTitle({ children }: { children: ReactNode }) {
  return <h3 className="kaus-modal-title">{children}</h3>;
}
export function ModalSub({ children }: { children: ReactNode }) {
  return <p className="mb-3 text-xs leading-relaxed" style={{ color: cream(55) }}>{children}</p>;
}
export function inputStyle(): CSSProperties {
  return { width: "100%", padding: "7px 9px", borderRadius: 8, border: `1px solid ${cream(12)}`, background: "var(--surface-raised)", color: "var(--midground-base)", font: "13px/1.5 var(--theme-font-sans)" };
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
    <div className="pointer-events-none fixed bottom-5 right-5 z-[80] flex flex-col gap-2">
      {items.map((t) => (
        <div
          key={t.id}
          role={t.tone === "bad" ? undefined : "status"}
          aria-live={t.tone === "bad" ? undefined : "polite"}
          aria-atomic="true"
          className={`rounded-lg px-3.5 py-2 text-xs shadow-lg${t.tone === "bad" ? " pointer-events-auto" : ""}`}
          style={{
            background: "var(--panel)",
            border: `1px solid ${t.tone === "bad" ? "color-mix(in srgb, var(--danger) 40%, transparent)" : cream(16)}`,
            color: t.tone === "bad" ? "var(--danger)" : "var(--midground-base)",
            boxShadow: "var(--shadow-popover)",
          }}
        >
          {t.tone === "bad" ? <ErrorNotice error={t.msg} /> : t.msg}
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
      <p data-modal-title className="mb-4 text-sm leading-relaxed" style={{ color: "var(--midground-base)" }}>{s.msg}</p>
      <div className="flex justify-end gap-3">
        <Button onClick={() => done(false)}>{t("ui.cancel")}</Button>
        <Button variant={s.danger ? "danger" : "primary"} onClick={() => done(true)}>{s.okLabel}</Button>
      </div>
    </Modal>
  );
}
