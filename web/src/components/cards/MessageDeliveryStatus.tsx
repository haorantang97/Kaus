import { useEffect, useId, useRef, useState } from "react";
import { CircleAlert, Clock3, RefreshCw, RotateCcw } from "lucide-react";

/** Shared receipt UI for local submissions and persisted messages. */
export function MessageDeliveryStatus({ state, label, diagnostic, recovery }: {
  state: "failed" | "unconfirmed";
  label: string;
  diagnostic?: string;
  recovery?: { label: string; title?: string; disabled?: boolean; onClick: () => void };
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const detailId = useId();
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !ref.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);

  return (
    <div ref={ref} className="kaus-msg-delivery" data-state={state}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}
      onKeyDown={event => {
        if (event.key === "Escape" && open) {
          event.stopPropagation();
          setOpen(false);
          toggle.current?.focus();
        }
      }}>
      <div className="kaus-msg-delivery-status" data-open={open || undefined}>
        <button ref={toggle} type="button" className="kaus-msg-action kaus-msg-delivery-toggle"
          aria-label={label} aria-expanded={open} aria-controls={detailId}
          onClick={() => setOpen(value => !value)}>
          {state === "failed" ? <CircleAlert size={15} aria-hidden="true" /> : <Clock3 size={15} aria-hidden="true" />}
        </button>
        <div id={detailId} className="kaus-msg-delivery-detail" role={open ? "note" : "tooltip"}>
          <span>{label}</span>
          {open && diagnostic && diagnostic !== label && <small>{diagnostic}</small>}
        </div>
      </div>
      {recovery && <button type="button" className="kaus-msg-action"
        disabled={recovery.disabled} aria-label={recovery.label} title={recovery.title ?? recovery.label}
        onClick={() => { setOpen(false); recovery.onClick(); }}>
        {state === "failed" ? <RotateCcw size={15} aria-hidden="true" /> : <RefreshCw size={15} aria-hidden="true" />}
      </button>}
    </div>
  );
}
