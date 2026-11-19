import { useRef, type KeyboardEvent } from "react";

/** Esc in an inline editor (instructions, memory, rename fields; 2026-11-19 HIG §5):
 *  an unchanged draft closes at once; a changed draft survives the first Esc and closes on a
 *  second one in a row, so one stray key never throws typing away. Any other key re-arms. */
export function useDraftEscape() {
  const armed = useRef(false);
  /** Call from onKeyDown. Returns true when it consumed an Escape. */
  return (event: KeyboardEvent, changed: boolean, discard: () => void): boolean => {
    if (event.key !== "Escape") { armed.current = false; return false; }
    if (event.nativeEvent.isComposing) return false;
    event.preventDefault();
    if (!changed || armed.current) { armed.current = false; discard(); }
    else armed.current = true;
    return true;
  };
}
