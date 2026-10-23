import { useEffect, useLayoutEffect, useRef, type CompositionEvent, type KeyboardEvent, type RefObject } from "react";

/** WebKit can emit a non-composing Enter immediately after committing a CJK word. */
export function useComposerInputGuard() {
  const composing = useRef(false);
  const compositionEndedAt = useRef<number | null>(null);
  const clearBeforeInput = useRef<(() => void) | null>(null);
  useEffect(() => () => clearBeforeInput.current?.(), []);

  function guardKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): boolean {
    clearBeforeInput.current?.();
    const native = event.nativeEvent;
    if (composing.current || native.isComposing || native.keyCode === 229) {
      compositionEndedAt.current = null;
      return true;
    }
    const ended = compositionEndedAt.current;
    compositionEndedAt.current = null;
    if (event.key !== "Enter" || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey || ended === null) return false;
    if (!/Apple Computer/.test(navigator.vendor) || Math.abs(native.timeStamp - ended) >= 500) return false;
    event.preventDefault();
    // Consume only the spurious line break that follows this same IME confirmation.
    const target = event.currentTarget;
    const events = ["beforeinput", "keydown", "keyup", "compositionstart", "focusout"];
    const cleanup = () => { for (const name of events) target.removeEventListener(name, intercept, true); clearBeforeInput.current = null; };
    const intercept = (next: Event) => {
      if (next === native) return;
      cleanup();
      if (next.type === "beforeinput" && ["insertParagraph", "insertLineBreak"].includes((next as InputEvent).inputType)) next.preventDefault();
    };
    clearBeforeInput.current = cleanup;
    for (const name of events) target.addEventListener(name, intercept, true);
    return true;
  }

  return {
    guardKeyDown,
    isComposing: () => composing.current,
    onCompositionStart: () => { composing.current = true; compositionEndedAt.current = null; clearBeforeInput.current?.(); },
    onCompositionEnd: (event: CompositionEvent<HTMLTextAreaElement>) => { composing.current = false; compositionEndedAt.current = event.nativeEvent.timeStamp; },
  };
}

/** Native field sizing where available, with the same limits in its JS fallback. */
export function useComposerAutosize(ref: RefObject<HTMLTextAreaElement | null>, value: string, enabled = true) {
  useLayoutEffect(() => {
    const input = ref.current;
    if (!input || !enabled) return;
    let previousWidth = -1;
    const resize = () => {
      const maxHeight = Math.max(44, Math.min(384, (window.visualViewport?.height ?? window.innerHeight) * .4));
      input.style.maxHeight = `${maxHeight}px`;
      input.style.height = "auto";
      if (typeof CSS !== "undefined" && CSS.supports?.("field-sizing", "content")) {
        input.style.overflowY = "auto";
      } else {
        input.style.height = `${Math.min(maxHeight, Math.max(44, input.scrollHeight))}px`;
        input.style.overflowY = input.scrollHeight > maxHeight ? "auto" : "hidden";
      }
    };
    resize();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width ?? input.clientWidth;
      if (width === previousWidth) return;
      previousWidth = width;
      resize();
    });
    observer?.observe(input);
    window.addEventListener("resize", resize);
    window.visualViewport?.addEventListener("resize", resize);
    return () => { observer?.disconnect(); window.removeEventListener("resize", resize); window.visualViewport?.removeEventListener("resize", resize); };
  }, [ref, value, enabled]);
}
