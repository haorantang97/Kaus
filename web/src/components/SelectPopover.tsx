import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import { useScrollFade } from "../hooks/useScrollFade";
import { createPortal } from "react-dom";
import { Check, ChevronDown, Search } from "lucide-react";
import { useLocale } from "../i18n";
import "./select-popover.css";

export type SelectPopoverOption = {
  value: string;
  label: string;
  description?: string;
  disabled?: boolean;
};

export type SelectPopoverProps = {
  id?: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: SelectPopoverOption[];
  placeholder?: string;
  searchable?: boolean;
  searchPlaceholder?: string;
  emptyLabel?: string;
  disabled?: boolean;
  className?: string;
  style?: CSSProperties;
};

const FOCUSABLE = 'a[href], button, input:not([type="hidden"]), select, textarea, [tabindex], [contenteditable="true"]';

function isTabStop(element: HTMLElement) {
  if (element.tabIndex < 0 || element.matches(':disabled, [aria-disabled="true"]') || element.closest('[data-kaus-select-menu], [hidden], [inert], [aria-hidden="true"]')) return false;
  for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
    const styles = getComputedStyle(ancestor);
    if (styles.display === "none" || styles.visibility === "hidden") return false;
  }
  return true;
}

/** An anchored selection menu. Its portal always opens below the trigger. */
export function SelectPopover({
  id, label, value, onChange, options, placeholder, searchable,
  searchPlaceholder, emptyLabel, disabled = false, className = "", style,
}: SelectPopoverProps) {
  const { locale } = useLocale();
  const generatedId = useId();
  const listId = `${id ?? generatedId}-options`;
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const focusPendingRef = useRef(false);
  const typeaheadRef = useRef({ text: "", time: 0 });
  const [open, setOpen] = useState(false);
  useScrollFade(listRef, open);
  const [query, setQuery] = useState("");
  const [activeValue, setActiveValue] = useState<string | null>(null);
  const [position, setPosition] = useState<CSSProperties | null>(null);
  const showSearch = searchable ?? options.length > 8;
  const selected = options.find(option => option.value === value);
  const needle = query.trim().toLocaleLowerCase();
  const shown = options.filter(option => !needle || `${option.label} ${option.description ?? ""} ${option.value}`.toLocaleLowerCase().includes(needle));
  const enabled = shown.filter(option => !option.disabled);
  const active = enabled.find(option => option.value === activeValue) ?? enabled[0];
  const activeIndex = active ? shown.indexOf(active) : -1;
  const activeId = activeIndex < 0 ? undefined : `${listId}-${activeIndex}`;
  const searchText = searchPlaceholder ?? (locale === "zh" ? "搜索选项" : "Search options");
  const portalHost = triggerRef.current?.closest<HTMLElement>('[role="dialog"], [data-kaus-modal]') ?? document.body;

  function close(returnFocus = true) {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus({ preventScroll: true });
  }

  function show(edge?: "first" | "last") {
    if (disabled || triggerRef.current?.matches(":disabled")) return;
    setQuery("");
    setPosition(null);
    focusPendingRef.current = true;
    const available = options.filter(option => !option.disabled);
    const initial = edge === "first" ? available[0] : edge === "last" ? available.at(-1) : available.find(option => option.value === value) ?? available[0];
    setActiveValue(initial?.value ?? null);
    typeaheadRef.current = { text: "", time: 0 };
    setOpen(true);
  }

  function choose(option: SelectPopoverOption) {
    if (option.disabled) return;
    close();
    if (option.value !== value) onChange(option.value);
  }

  useLayoutEffect(() => {
    if (!open || disabled) return;
    const trigger = triggerRef.current;
    if (!trigger) return;
    function place(allowScroll = false) {
      if (!trigger) return;
      const viewport = window.visualViewport;
      const width = viewport?.width ?? window.innerWidth;
      const height = viewport?.height ?? window.innerHeight;
      const leftEdge = viewport?.offsetLeft ?? 0;
      const topEdge = viewport?.offsetTop ?? 0;
      let rect = trigger.getBoundingClientRect();
      const desired = Math.min(360, Math.max(60, options.length * 36 + 12 + (showSearch ? 48 : 0)));
      if (allowScroll && topEdge + height - rect.bottom - 14 < Math.min(desired, 180)) {
        trigger.scrollIntoView?.({ block: "center", inline: "nearest", behavior: "instant" });
        rect = trigger.getBoundingClientRect();
      }
      const menuWidth = Math.min(Math.max(260, rect.width), Math.max(0, width - 16));
      setPosition({
        top: rect.bottom + 6,
        left: Math.max(leftEdge + 8, Math.min(rect.left, leftEdge + width - menuWidth - 8)),
        width: menuWidth,
        maxHeight: Math.max(0, Math.min(360, topEdge + height - rect.bottom - 14)),
      });
    }
    place(true);
    const reposition = (event?: Event) => {
      if (event?.target instanceof Node && menuRef.current?.contains(event.target)) return;
      place();
    };
    const resize = () => place(true);
    window.addEventListener("resize", resize);
    window.addEventListener("scroll", reposition, true);
    window.visualViewport?.addEventListener("resize", resize);
    window.visualViewport?.addEventListener("scroll", reposition);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => place());
    observer?.observe(trigger);
    return () => {
      window.removeEventListener("resize", resize);
      window.removeEventListener("scroll", reposition, true);
      window.visualViewport?.removeEventListener("resize", resize);
      window.visualViewport?.removeEventListener("scroll", reposition);
      observer?.disconnect();
    };
  }, [open, disabled, options.length, showSearch]);

  useLayoutEffect(() => {
    // The portal is hidden until its first measured position has committed.
    if (!open || !position || !focusPendingRef.current) return;
    focusPendingRef.current = false;
    (showSearch ? searchRef.current : listRef.current)?.focus({ preventScroll: true });
  }, [open, position, showSearch]);

  useEffect(() => {
    if (disabled && open) setOpen(false);
  }, [disabled, open]);

  useEffect(() => {
    if (!open) return;
    const dismissOutside = (event: Event) => {
      const target = event.target;
      if (!(target instanceof Node) || triggerRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", dismissOutside, true);
    document.addEventListener("focusin", dismissOutside);
    return () => {
      document.removeEventListener("pointerdown", dismissOutside, true);
      document.removeEventListener("focusin", dismissOutside);
    };
  }, [open]);

  useLayoutEffect(() => {
    if (!open || !position || !activeId || !listRef.current) return;
    const row = document.getElementById(activeId)?.getBoundingClientRect();
    const list = listRef.current.getBoundingClientRect();
    if (!row) return;
    if (row.top < list.top) listRef.current.scrollTop += row.top - list.top;
    else if (row.bottom > list.bottom) listRef.current.scrollTop += row.bottom - list.bottom;
  }, [open, activeId, position?.maxHeight]);

  function onMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.key === "Tab") {
      // Follow the trigger's document order, not the portal at the end of body.
      const dialog = triggerRef.current?.closest<HTMLElement>('[role="dialog"], [data-kaus-modal]');
      const stops = Array.from((dialog ?? document).querySelectorAll<HTMLElement>(FOCUSABLE)).filter(isTabStop);
      const current = stops.indexOf(triggerRef.current!);
      const successor = (current >= 0 ? stops[current + (event.shiftKey ? -1 : 1)] : undefined)
        ?? (dialog ? stops[event.shiftKey ? stops.length - 1 : 0] : undefined);
      close(false);
      if (successor) {
        event.preventDefault();
        successor.focus();
      } else {
        // Let the browser leave the document normally at either boundary.
        triggerRef.current?.focus({ preventScroll: true });
      }
      event.stopPropagation();
      return;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      const current = active ? enabled.indexOf(active) : -1;
      const next = event.key === "Home" ? 0 : event.key === "End" ? enabled.length - 1 :
        (current + (event.key === "ArrowDown" ? 1 : -1) + enabled.length) % enabled.length;
      setActiveValue(enabled[next]?.value ?? null);
      return;
    }
    if (event.key === "Enter" || (event.key === " " && event.target !== searchRef.current)) {
      event.preventDefault();
      event.stopPropagation();
      if (active) choose(active);
      return;
    }
    if (!showSearch && event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
      event.preventDefault();
      event.stopPropagation();
      const time = Date.now();
      const previous = time - typeaheadRef.current.time < 700 ? typeaheadRef.current.text : "";
      const text = `${previous}${event.key.toLocaleLowerCase()}`;
      typeaheadRef.current = { text, time };
      const prefix = Array.from(text).every(character => character === text[0]) ? text[0] : text;
      const current = active ? enabled.indexOf(active) : -1;
      const ordered = prefix.length === 1 ? [...enabled.slice(current + 1), ...enabled.slice(0, current + 1)] : enabled;
      const match = ordered.find(option => option.label.toLocaleLowerCase().startsWith(prefix));
      if (match) setActiveValue(match.value);
    }
  }

  return <span className={`kaus-select ${className}`} style={style}>
    <button
      ref={triggerRef}
      id={id}
      type="button"
      role="combobox"
      aria-label={label}
      aria-haspopup="listbox"
      aria-controls={open ? listId : undefined}
      aria-expanded={open}
      data-kaus-select-trigger="true"
      disabled={disabled}
      className="kaus-select-trigger"
      onClick={() => open ? close() : show()}
      onKeyDown={event => {
        if (open && event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          close();
          return;
        }
        if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          event.stopPropagation();
          show(event.key === "Home" ? "first" : event.key === "End" || (event.key === "ArrowUp" && !selected) ? "last" : undefined);
        }
      }}
    >
      <span className={`kaus-select-value${selected ? "" : " is-placeholder"}`}>{selected?.label ?? placeholder ?? label}</span>
      <ChevronDown size={14} aria-hidden="true" />
    </button>
    {open && !disabled && createPortal(<div
      ref={menuRef}
      className="kaus-select-menu"
      data-kaus-select-menu={listId}
      style={position ?? { visibility: "hidden" }}
      onKeyDown={onMenuKeyDown}
    >
      {showSearch && <div className="kaus-select-search">
        <Search size={14} aria-hidden="true" />
        <input
          ref={searchRef}
          type="search"
          aria-label={`${label} · ${searchText}`}
          aria-controls={listId}
          aria-activedescendant={activeId}
          placeholder={searchText}
          value={query}
          onChange={event => { setQuery(event.target.value); setActiveValue(null); }}
          autoComplete="off"
          spellCheck={false}
        />
      </div>}
      <div ref={listRef} id={listId} className="kaus-select-options kaus-scroll-fade" role="listbox" aria-label={label} aria-activedescendant={activeId} tabIndex={-1}>
        {shown.map((option, index) => <div
          key={option.value}
          id={`${listId}-${index}`}
          role="option"
          aria-label={option.label}
          aria-description={option.description}
          aria-selected={option.value === value}
          aria-disabled={option.disabled || undefined}
          data-active={option.value === active?.value || undefined}
          className="kaus-select-option"
          onPointerMove={() => { if (!option.disabled) setActiveValue(option.value); }}
          onPointerDown={event => event.preventDefault()}
          onClick={() => choose(option)}
        >
          <span className="kaus-select-option-copy"><span>{option.label}</span>{option.description && <small>{option.description}</small>}</span>
          <span className="kaus-select-check" aria-hidden="true">{option.value === value && <Check size={14} />}</span>
        </div>)}
        {!shown.length && <div className="kaus-select-empty" role="status">{emptyLabel ?? (locale === "zh" ? "没有匹配选项" : "No matching options")}</div>}
      </div>
    </div>, portalHost)}
  </span>;
}
