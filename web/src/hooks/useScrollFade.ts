import { useLayoutEffect, type RefObject } from "react";

/** Fade only edges with more content; resize and streamed children can change both. */
export function useScrollFade(ref: RefObject<HTMLElement | null>, enabled = true) {
  useLayoutEffect(() => {
    const element = ref.current;
    if (!enabled || !element) return;
    const update = () => {
      const top = element.scrollTop > 2 ? "true" : "false";
      const bottom = element.scrollHeight - element.clientHeight - element.scrollTop > 2 ? "true" : "false";
      if (element.dataset.fadeTop !== top) element.dataset.fadeTop = top;
      if (element.dataset.fadeBottom !== bottom) element.dataset.fadeBottom = bottom;
    };
    update();
    element.addEventListener("scroll", update, { passive: true });
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    resize?.observe(element);
    const mutation = new MutationObserver(update);
    mutation.observe(element, { childList: true, subtree: true, characterData: true });
    return () => { element.removeEventListener("scroll", update); resize?.disconnect(); mutation.disconnect(); };
  }, [ref, enabled]);
}
