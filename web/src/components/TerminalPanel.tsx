import { useEffect, useRef } from "react";
import { PanelRightClose } from "lucide-react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

import { useLocale } from "../i18n";
import { bootstrapSessionAuth } from "../lib/authorizedFetch";
import { SidePanelFrame } from "./SidePanelFrame";
import "./side-panel.css";

/** Close codes of `WS /conversations/{id}/terminal` (kernel `session_routes/terminal.py`). */
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_NO_WORKSPACE = 4409;

export function terminalUrl(conversationId: string, at: Location = window.location): string {
  return `${at.protocol === "https:" ? "wss" : "ws"}://${at.host}/api/conversations/${encodeURIComponent(conversationId)}/terminal`;
}

/** Colours from the panel's own computed style, so the terminal follows the app theme. */
function themeOf(element: HTMLElement) {
  const style = getComputedStyle(element);
  const background = style.backgroundColor && style.backgroundColor !== "rgba(0, 0, 0, 0)" ? style.backgroundColor : undefined;
  const foreground = style.color || undefined;
  return { background, foreground, cursor: foreground, selectionBackground: "rgba(128, 128, 128, 0.32)" };
}

/** Terminal panel (2026-11-23「页头面板」⑥, see product-decisions): a plain shell in this
 *  conversation's working directory. It does not take over the conversation. Closing the
 *  panel leaves the shell running; reopening shows its recent output. */
export function TerminalPanel({ conversationId, onClose }: { conversationId: string; onClose: () => void }) {
  const { t } = useLocale();
  const host = useRef<HTMLDivElement>(null);
  const messages = useRef({ exited: t("panel.terminal.exited"), disconnected: t("panel.terminal.disconnected"), noWorkspace: t("panel.files.noWorkspace") });
  messages.current = { exited: t("panel.terminal.exited"), disconnected: t("panel.terminal.disconnected"), noWorkspace: t("panel.files.noWorkspace") };

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const term = new Terminal({
      fontFamily: "'JetBrains Mono', ui-monospace, Menlo, monospace",
      fontSize: 12,
      lineHeight: 1.25,
      cursorBlink: true,
      scrollback: 5000,
      allowProposedApi: false,
      theme: themeOf(element),
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(element);
    const refit = () => { try { fit.fit(); } catch { /* not laid out yet */ } };
    refit();

    let socket: WebSocket | null = null;
    let disposed = false;
    let ended = false;
    let retriedAuth = false;
    const send = (frame: object) => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame)); };
    const note = (text: string) => term.write(`\r\n\x1b[2m${text}\x1b[0m\r\n`);

    const connect = async (forceToken = false) => {
      ended = false;
      let token: string;
      try { token = await bootstrapSessionAuth(forceToken); } catch { ended = true; note(messages.current.disconnected); return; }
      if (disposed) return;
      const ws = new WebSocket(terminalUrl(conversationId));
      ws.binaryType = "arraybuffer";
      socket = ws;
      ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token, cols: term.cols, rows: term.rows }));
      ws.onmessage = event => {
        if (typeof event.data === "string") {
          try { if ((JSON.parse(event.data) as { type?: string }).type === "exit") { ended = true; note(messages.current.exited); } } catch { /* ignore */ }
          return;
        }
        term.write(new Uint8Array(event.data as ArrayBuffer));
      };
      ws.onclose = event => {
        if (disposed || socket !== ws) return;
        socket = null;
        if (event.code === CLOSE_UNAUTHORIZED && !retriedAuth) { retriedAuth = true; void connect(true); return; }
        if (ended) return;
        ended = true;
        note(event.code === CLOSE_NO_WORKSPACE ? messages.current.noWorkspace : messages.current.disconnected);
      };
    };

    const input = term.onData(data => {
      if (ended) { term.reset(); retriedAuth = false; void connect(); return; }
      send({ type: "input", data });
    });
    const binary = term.onBinary(data => {
      if (!ended) send({ type: "input", data, binary: true });
    });
    const resized = term.onResize(({ cols, rows }) => send({ type: "resize", cols, rows }));
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(refit);
    observer?.observe(element);
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    const retheme = () => { term.options.theme = themeOf(element); };
    media?.addEventListener?.("change", retheme);
    const themeObserver = new MutationObserver(retheme);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });

    void connect();
    term.focus();
    return () => {
      disposed = true;
      input.dispose(); binary.dispose(); resized.dispose();
      observer?.disconnect(); themeObserver.disconnect();
      media?.removeEventListener?.("change", retheme);
      socket?.close();
      term.dispose();
    };
  }, [conversationId]);

  return (
    <SidePanelFrame label={t("panel.terminal")} testId="terminal-panel">
      <div className="kaus-side-panel-header">
        <strong>{t("panel.terminal")}</strong>
        <button type="button" className="kaus-chat-icon-action" aria-label={t("panel.close")} title={t("panel.close")} onClick={onClose}>
          <PanelRightClose size={16} aria-hidden />
        </button>
      </div>
      <div ref={host} className="kaus-terminal-host" />
    </SidePanelFrame>
  );
}
