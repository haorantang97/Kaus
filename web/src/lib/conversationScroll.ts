export interface MessageAnchorOffset { id: string; top: number }

/** Anchor offsets are measured on layout changes, never during ordinary scrolling. */
export function readingAnchor(offsets: MessageAnchorOffset[], line: number): string | null {
  let low = 0;
  let high = offsets.length - 1;
  let selected = 0;
  while (low <= high) {
    const middle = (low + high) >>> 1;
    if (offsets[middle].top <= line) { selected = middle; low = middle + 1; }
    else high = middle - 1;
  }
  return offsets[selected]?.id ?? null;
}

/** A bounded, interruptible scroll. The destination can follow streaming layout changes. */
export function scrollConversation(
  scroller: HTMLElement,
  destination: () => number,
  settled: (completed: boolean) => void,
): () => void {
  const target = () => Math.max(0, Math.min(destination(), Math.max(0, scroller.scrollHeight - scroller.clientHeight)));
  const from = scroller.scrollTop;
  const distance = Math.abs(target() - from);
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  if (reduced || distance < 1) {
    scroller.scrollTo({ top: target(), behavior: "auto" });
    settled(true);
    return () => {};
  }
  const duration = Math.min(460, 180 + Math.sqrt(distance) * 5);
  // Use the frame clock throughout (also works across embedded WebView realms).
  let started: number | null = null;
  let frame = 0;
  let stopped = false;
  const tick = (now: number) => {
    if (stopped) return;
    started ??= now;
    const progress = Math.min(1, Math.max(0, (now - started) / duration));
    const eased = 1 - (1 - progress) ** 3;
    scroller.scrollTo({ top: from + (target() - from) * eased, behavior: "auto" });
    if (progress < 1) frame = requestAnimationFrame(tick);
    else { stopped = true; settled(true); }
  };
  scroller.scrollTo({ top: from, behavior: "auto" });
  frame = requestAnimationFrame(tick);
  return () => {
    if (stopped) return;
    stopped = true;
    cancelAnimationFrame(frame);
    settled(false);
  };
}
