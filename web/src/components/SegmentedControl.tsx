import { useId, type CSSProperties } from "react";

export function SegmentedControl<T extends string>({ label, value, options, onChange }: {
  label: string; value: T; options: { value: T; label: string; disabled?: boolean }[]; onChange: (value: T) => void;
}) {
  const id = useId();
  const index = Math.max(0, options.findIndex(option => option.value === value));
  return <div role="group" aria-label={label} className="kaus-segmented" style={{ "--segment-count": options.length, "--segment-index": index } as CSSProperties}>
    <span className="kaus-segmented-indicator" aria-hidden="true" />
    {options.map(option => <button key={option.value} type="button" id={`${id}-${option.value}`} disabled={option.disabled}
      aria-pressed={option.value === value} tabIndex={option.value === value ? 0 : -1} onClick={() => onChange(option.value)}
      onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
        const available = options.filter(item => !item.disabled);
        const current = available.findIndex(item => item.value === option.value);
        const next = event.key === "Home" ? 0 : event.key === "End" ? available.length - 1 : ["ArrowRight", "ArrowDown"].includes(event.key) ? (current + 1) % available.length : ["ArrowLeft", "ArrowUp"].includes(event.key) ? (current - 1 + available.length) % available.length : -1;
        if (next < 0) return;
        event.preventDefault();
        onChange(available[next].value);
        document.getElementById(`${id}-${available[next].value}`)?.focus();
      }}>{option.label}</button>)}
  </div>;
}
