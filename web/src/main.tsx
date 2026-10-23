import { Component, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./index.css";
import "./theme-overrides.css";
import { App } from "./App";
import { TerminalLab } from "./components/TerminalLab";
import { CardsPreview } from "./components/cards/preview";
import "./styles/controls.css";
import { currentLocale } from "./i18n";
import { failureDiagnostic } from "./lib/userFacingErrors";

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
    const message = failureDiagnostic(this.state.error);
    const zh = currentLocale() === "zh";
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
          <h1 style={{ margin: "0 0 14px", fontSize: 20, fontWeight: 600 }}>{zh ? "界面暂时无法显示" : "Unable to display this view"}</h1>
          <details>
            <summary style={{ fontSize: 12, cursor: "pointer", color: "color-mix(in srgb, var(--midground-base) 55%, transparent)" }}>{zh ? "详情" : "Details"}</summary>
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
          </details>
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
              {zh ? "重试" : "Try again"}
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
              {zh ? "重新加载" : "Reload"}
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
