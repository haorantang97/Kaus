import { useId, useLayoutEffect, useRef } from "react";

const layers: { root: HTMLElement; content: HTMLElement }[] = [];
let bodyOverflow = "";
const focusables = 'button, a[href], iframe, input:not([type="hidden"]), select, textarea, summary, [tabindex], [contenteditable="true"]';

function enabled(element: HTMLElement) {
  if (element.tabIndex < 0 || element.matches(':disabled, [aria-disabled="true"]') || element.closest('[inert], [hidden], [aria-hidden="true"]')) return false;
  for (let parent: HTMLElement | null = element; parent; parent = parent.parentElement) {
    const style = getComputedStyle(parent);
    if (style.display === "none" || style.visibility === "hidden") return false;
  }
  return true;
}

function syncLayers() {
  layers.forEach((layer, index) => {
    const inactive = index !== layers.length - 1;
    layer.root.inert = inactive;
    if (inactive) layer.root.setAttribute("aria-hidden", "true");
    else layer.root.removeAttribute("aria-hidden");
    layer.root.toggleAttribute("data-nested-dialog-open", inactive);
  });
}

/** Modal behavior specified in open-claude-ui v1 §2.4 / §6.6, adapted to Kaus portals. */
export function useModalLayer(onClose: () => void) {
  // Capture before the commit phase, when React autoFocus children can steal focus.
  const openerRef = useRef<HTMLElement | null>(typeof document !== "undefined" && document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const rootRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const headingId = useId();

  useLayoutEffect(() => {
    const root = rootRef.current;
    const content = contentRef.current;
    if (!root || !content) return;
    const opener = openerRef.current;
    const layer = { root, content };
    const dialog = content.querySelector<HTMLElement>('[role="dialog"], [role="alertdialog"]') ?? content;
    if (dialog === content) {
      dialog.setAttribute("role", "dialog");
      dialog.setAttribute("aria-modal", "true");
    }
    if (!dialog.hasAttribute("aria-label") && !dialog.hasAttribute("aria-labelledby")) {
      const heading = dialog.querySelector<HTMLElement>("h1, h2, h3, h4, [data-modal-title]");
      if (heading) { heading.id ||= headingId; dialog.setAttribute("aria-labelledby", heading.id); }
    }
    if (!layers.length) { bodyOverflow = document.body.style.overflow; document.body.style.overflow = "hidden"; }
    layers.push(layer);
    syncLayers();
    const isTop = () => layers.at(-1) === layer;
    const ownedSkillMenus = () => {
      const owners = new Set(Array.from(content.querySelectorAll<HTMLElement>("[data-skill-tree-owner]")).map(node => node.dataset.skillTreeOwner));
      return Array.from(document.querySelectorAll<HTMLElement>(".skill-tree-menu[data-skill-tree-owner]")).filter(node => owners.has(node.dataset.skillTreeOwner));
    };
    const contains = (target: Node | null) => !!target && (content.contains(target) || ownedSkillMenus().some(menu => menu.contains(target)));
    const hasChildMenu = () => !!content.querySelector('.kaus-bar-menu > button[aria-expanded="true"], [data-kaus-select-trigger][aria-expanded="true"], [data-kaus-engine-config-trigger][aria-expanded="true"]') || ownedSkillMenus().length > 0;
    const first = () => content.querySelector<HTMLElement>("[data-autofocus], [autofocus]")
      ?? Array.from(content.querySelectorAll<HTMLElement>(focusables)).find(enabled)
      ?? content;
    let lastFocus: HTMLElement = first();
    // autoFocus inputs have already claimed focus during React's commit.
    if (!contains(document.activeElement)) lastFocus.focus({ preventScroll: true });
    const onFocus = (event: FocusEvent) => {
      if (!isTop()) return;
      const target = event.target;
      if (target instanceof HTMLElement && contains(target)) { lastFocus = target; return; }
      (lastFocus.isConnected && contains(lastFocus) ? lastFocus : first()).focus({ preventScroll: true });
    };
    const onKey = (event: KeyboardEvent) => {
      if (!isTop() || event.isComposing || event.keyCode === 229) return;
      if (hasChildMenu()) return;
      if (event.key === "Escape" && content.querySelector('[data-pane-dragging="true"]')) return;
      if (event.key === "Escape") {
        event.preventDefault(); event.stopImmediatePropagation(); closeRef.current();
      } else if (event.key === "Tab") {
        const items = Array.from(content.querySelectorAll<HTMLElement>(focusables)).filter(enabled);
        const index = items.indexOf(document.activeElement as HTMLElement);
        if (!items.length || (event.shiftKey ? index <= 0 : index < 0 || index === items.length - 1)) {
          event.preventDefault();
          (items[event.shiftKey ? items.length - 1 : 0] ?? content).focus({ preventScroll: true });
        }
      }
    };
    document.addEventListener("focusin", onFocus);
    window.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("focusin", onFocus);
      window.removeEventListener("keydown", onKey, true);
      const wasTop = isTop();
      const index = layers.indexOf(layer);
      if (index >= 0) layers.splice(index, 1);
      syncLayers();
      if (!layers.length) document.body.style.overflow = bodyOverflow;
      if (wasTop && opener?.isConnected && !opener.closest("[inert]")) opener.focus({ preventScroll: true });
    };
  }, [headingId]);

  return { rootRef, contentRef };
}
