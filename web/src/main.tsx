import { Component, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./index.css";
import "./theme-overrides.css";
import { App } from "./App";
import { TerminalLab } from "./components/TerminalLab";
import { CardsPreview } from "./components/cards/preview";

class DashboardErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[dashboard] render failure", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    const message = this.state.error.message || String(this.state.error);
    return (
      <div
        style={{
          minHeight: "100dvh",
          display: "grid",
          placeItems: "center",
          padding: 24,
          background: "var(--background-base)",
          color: "var(--midground-base)",
          fontFamily: "var(--theme-font-ui)",
        }}
      >
        <section
          style={{
            width: "min(620px, 100%)",
            border: "1px solid color-mix(in srgb, var(--midground-base) 14%, transparent)",
            background: "var(--panel)",
            borderRadius: 14,
            padding: 24,
            boxShadow: "var(--shadow-lg)",
          }}
        >
          <div
            style={{
              color: "var(--accent-text)",
              fontFamily: "var(--theme-font-mono)",
              fontSize: 11,
              letterSpacing: ".16em",
              textTransform: "uppercase",
              marginBottom: 8,
            }}
          >
            Dashboard Render Guard
          </div>
          <h1 style={{ margin: "0 0 10px", fontSize: 22, fontWeight: 800 }}>The interface hit a rendering error</h1>
          <p style={{ margin: "0 0 14px", color: "color-mix(in srgb, var(--midground-base) 62%, transparent)", lineHeight: 1.6 }}>
            Backend services and PTY sessions are not stopped by this error. Try recovering the view first; if it repeats, send the error below to the maintenance agent.
          </p>
          <pre
            style={{
              maxHeight: 160,
              overflow: "auto",
              border: "1px solid color-mix(in srgb, var(--danger) 24%, transparent)",
              borderRadius: 8,
              padding: 12,
              whiteSpace: "pre-wrap",
              color: "var(--danger)",
              font: "12px/1.5 var(--theme-font-mono)",
            }}
          >
            {message}
          </pre>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 16 }}>
            <button
              type="button"
              onClick={() => this.setState({ error: null })}
              style={{
                border: "1px solid color-mix(in srgb, var(--midground-base) 16%, transparent)",
                borderRadius: 8,
                padding: "7px 12px",
                color: "var(--midground-base)",
                background: "transparent",
              }}
            >
              Recover View
            </button>
            <button
              type="button"
              onClick={() => window.location.reload()}
              style={{
                border: "1px solid color-mix(in srgb, var(--gold) 40%, transparent)",
                borderRadius: 8,
                padding: "7px 12px",
                color: "var(--accent-text)",
                background: "var(--gold-soft)",
              }}
            >
              Reload
            </button>
          </div>
        </section>
      </div>
    );
  }
}

/* 路由分流。`/dev/cards` 是卡片状态一览，**只在开发环境挂载**：生产构建里
   import.meta.env.DEV 为 false，这段判断被摇掉，预览页不进产物。 */
function root() {
  const path = window.location.pathname;
  if (path === "/terminal-lab") return <TerminalLab />;
  if (import.meta.env.DEV && path === "/dev/cards") return <CardsPreview />;
  // 真路由（IA §6.4，采纳 react-router）。App 内部只读 location，
  // flag 关闭时它一条路由分支都不会走——界面与改造前一致。
  return (
    <BrowserRouter>
      <App />
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <DashboardErrorBoundary>{root()}</DashboardErrorBoundary>,
);
