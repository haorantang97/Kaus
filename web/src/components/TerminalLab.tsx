import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import "@xterm/xterm/css/xterm.css";
import { fetchNetwork, type NetworkResp } from "../lib/api";
import { t } from "../i18n";

type ConnectionState = "idle" | "connecting" | "open" | "closed" | "error";

const SCROLLBACK_LINES = 100_000;
const DEFAULT_ROWS = 30;
const DEFAULT_COLS = 100;

function terminalLabWsOrigin() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  if (location.hostname === "localhost" && location.port === "5174") {
    return `${proto}://127.0.0.1:8877`;
  }
  if (location.hostname === "127.0.0.1" && location.port === "5174") {
    return `${proto}://127.0.0.1:8877`;
  }
  return `${proto}://${location.host}`;
}

function initialAgent() {
  const fromUrl = new URLSearchParams(window.location.search).get("agent");
  const fromStorage = window.localStorage.getItem("hermes-terminal-lab-agent");
  return fromUrl || fromStorage || "default";
}

export function TerminalLab() {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<XTerm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const resizeTimerRef = useRef<number | null>(null);
  const reconnectTokenRef = useRef(0);
  const [net, setNet] = useState<NetworkResp | null>(null);
  const [agent, setAgent] = useState(initialAgent);
  const [mode, setMode] = useState<"continue" | "new">("continue");
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [message, setMessage] = useState<string | null>(null);

  const agents = useMemo(() => {
    if (!net) return [];
    return Object.values(net.nodes)
      .filter((node) => !node.is_draft && !node.effective_killed)
      .sort((a, b) => (a.label || a.name).localeCompare(b.label || b.name));
  }, [net]);

  useEffect(() => {
    fetchNetwork()
      .then((next) => {
        setNet(next);
        if (!next.nodes[agent]) setAgent("default");
      })
      .catch((err) => setMessage(`网络数据加载失败：${String(err?.message ?? err)}`));
  }, [agent]);

  useEffect(() => {
    window.localStorage.setItem("hermes-terminal-lab-agent", agent);
  }, [agent]);

  const sendResize = useCallback(() => {
    const term = termRef.current;
    const fit = fitRef.current;
    const ws = wsRef.current;
    const host = hostRef.current;
    if (!term || !fit || !host) return;
    if (host.clientWidth < 20 || host.clientHeight < 20) return;
    try {
      fit.fit();
    } catch {
      return;
    }
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(`\x1b[RESIZE:${term.cols};${term.rows}]`);
    }
  }, []);

  const scheduleResize = useCallback(() => {
    if (resizeTimerRef.current !== null) window.clearTimeout(resizeTimerRef.current);
    resizeTimerRef.current = window.setTimeout(() => {
      resizeTimerRef.current = null;
      sendResize();
    }, 40);
  }, [sendResize]);

  const closeConnection = useCallback(() => {
    reconnectTokenRef.current += 1;
    try {
      wsRef.current?.close(1000, "terminal lab close");
    } catch {
      // no-op
    }
    wsRef.current = null;
    setConnection("closed");
  }, []);

  const connect = useCallback(() => {
    const term = termRef.current;
    if (!term) return;
    closeConnection();
    const token = reconnectTokenRef.current + 1;
    reconnectTokenRef.current = token;
    setMessage(null);
    setConnection("connecting");
    term.reset();
    term.writeln("\x1b[90m[Terminal Lab] starting thin PTY bridge...\x1b[0m");

    const params = new URLSearchParams({
      rows: String(term.rows || DEFAULT_ROWS),
      cols: String(term.cols || DEFAULT_COLS),
    });
    if (mode === "new") params.set("new", "1");
    else params.set("continue", "1");

    const ws = new WebSocket(
      `${terminalLabWsOrigin()}/ws/terminal-lab/${encodeURIComponent(agent)}?${params.toString()}`,
    );
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    const decoder = new TextDecoder();

    ws.onopen = () => {
      if (reconnectTokenRef.current !== token) return;
      setConnection("open");
      sendResize();
      term.focus();
    };

    ws.onmessage = (event) => {
      if (reconnectTokenRef.current !== token) return;
      if (typeof event.data === "string") {
        term.write(event.data);
      } else {
        term.write(decoder.decode(new Uint8Array(event.data as ArrayBuffer), { stream: true }));
      }
    };

    ws.onerror = () => {
      if (reconnectTokenRef.current !== token) return;
      setConnection("error");
      setMessage("WebSocket 连接错误。确认后端 :8877 已重启并包含 /ws/terminal-lab。");
    };

    ws.onclose = () => {
      if (reconnectTokenRef.current !== token) return;
      wsRef.current = null;
      setConnection((prev) => (prev === "error" ? "error" : "closed"));
      term.writeln("\r\n\x1b[90m[Terminal Lab] PTY closed.\x1b[0m");
    };
  }, [agent, closeConnection, mode, sendResize]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const term = new XTerm({
      allowProposedApi: true,
      allowTransparency: false,
      convertEol: false,
      cursorBlink: true,
      cursorStyle: "block",
      disableStdin: false,
      drawBoldTextInBrightColors: true,
      fontFamily:
        'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, "PingFang SC", "Noto Sans Mono CJK SC", monospace',
      fontSize: 14,
      lineHeight: 1.18,
      macOptionIsMeta: true,
      scrollback: SCROLLBACK_LINES,
      scrollOnUserInput: false,
      theme: {
        background: "#11140c",
        foreground: "#eeeadd",
        cursor: "#cbee52",
        cursorAccent: "#11140c",
        selectionBackground: "#cbee5244",
        black: "#11140c",
        red: "#d98a55",
        green: "#86c64f",
        yellow: "#cbee52",
        blue: "#91a7c6",
        magenta: "#c39bc0",
        cyan: "#86c0bc",
        white: "#edeadd",
        brightBlack: "#807c72",
        brightRed: "#e8a088",
        brightGreen: "#b0cf9a",
        brightYellow: "#e8c873",
        brightBlue: "#9db8e0",
        brightMagenta: "#d4b0d0",
        brightCyan: "#9dd6d2",
        brightWhite: "#fbf6ea",
      },
    });
    const fit = new FitAddon();
    const unicode = new Unicode11Addon();
    term.loadAddon(fit);
    term.loadAddon(unicode);
    term.unicode.activeVersion = "11";
    term.open(host);
    termRef.current = term;
    fitRef.current = fit;
    requestAnimationFrame(() => {
      sendResize();
      term.focus();
    });

    const inputDisposable = term.onData((data) => {
      const ws = wsRef.current;
      if (ws?.readyState === WebSocket.OPEN) ws.send(data);
    });
    const resizeDisposable = term.onResize(({ cols, rows }) => {
      const ws = wsRef.current;
      if (ws?.readyState === WebSocket.OPEN) ws.send(`\x1b[RESIZE:${cols};${rows}]`);
    });
    const ro = new ResizeObserver(scheduleResize);
    ro.observe(host);
    window.addEventListener("resize", scheduleResize);

    return () => {
      inputDisposable.dispose();
      resizeDisposable.dispose();
      ro.disconnect();
      window.removeEventListener("resize", scheduleResize);
      if (resizeTimerRef.current !== null) window.clearTimeout(resizeTimerRef.current);
      closeConnection();
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
  }, [closeConnection, scheduleResize, sendResize]);

  return (
    <div className="terminal-lab-page">
      <header className="terminal-lab-toolbar">
        <div>
          <div className="terminal-lab-kicker">{t("lab.kicker")}</div>
          <h1>薄终端实验室</h1>
        </div>
        <label>
          Agent
          <select value={agent} onChange={(event) => setAgent(event.target.value)}>
            {agents.length === 0 ? <option value={agent}>{agent}</option> : null}
            {agents.map((node) => (
              <option key={node.name} value={node.name}>
                {node.label || node.name} · {node.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          启动
          <select value={mode} onChange={(event) => setMode(event.target.value as "continue" | "new")}>
            <option value="continue">续接最近会话</option>
            <option value="new">新会话</option>
          </select>
        </label>
        <button type="button" onClick={connect} disabled={connection === "connecting"}>
          {connection === "open" ? "重启连接" : "连接"}
        </button>
        <button type="button" onClick={closeConnection}>
          关闭 PTY
        </button>
        <a href="/" className="terminal-lab-link">返回 dashboard</a>
        <span className={`terminal-lab-state is-${connection}`}>{connection}</span>
      </header>
      {message && <div className="terminal-lab-message">{message}</div>}
      <main className="terminal-lab-shell" onMouseDown={() => termRef.current?.focus()}>
        <div ref={hostRef} className="terminal-lab-host" />
      </main>
    </div>
  );
}
