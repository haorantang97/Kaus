import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ChevronDown, Clipboard, ExternalLink, History, Moon, Plus, RotateCw, Sun, Trash2, X } from "lucide-react";
import { apiDelete, apiGet, apiPost, fetchSessionHealth, fetchSessions, getDashboardConfig, openTerminal, setDashboardConfig, type DashboardConfig, type OrgNode, type Session, type SessionHealth } from "../lib/api";
import { confirmAsync, cream, dname, toast } from "./ui";
import { t } from "../i18n";

interface KnownModel { default: string; provider?: string | null; base_url?: string | null; context_window?: number; reasoning_levels?: string[]; fast_mode?: boolean; family?: string; tier?: string; description?: string }
interface Lever {
  model: string | { default?: string } | null;
  model_own: boolean;
  model_reasoning_levels: string[];
  model_fast_mode: boolean;
  fast_mode: boolean;
  reasoning_effort: string;
  reasoning_own: boolean;
  approvals_mode: "manual" | "smart" | "off";
  approvals_own: boolean;
  known_models: KnownModel[];
}
interface LeverUpdate {
  effective_model: string | KnownModel | null;
  effective_reasoning_effort: string;
  effective_fast_mode: boolean;
}
interface ConversationPrefs {
  active?: string | null;
}

const DASHBOARD_DIR = "~/.hermes/dashboard";
const CONVERSATION_PREFS_KEY = "hermes-dashboard-conversation-prefs-real-terminal-v1";
const REASONING_LABELS: Record<string, string> = {
  none: "关闭",
  minimal: "最低",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "极高",
};
/* 危险命令审批的取值叫法（批次十一第 3 件）：全站一组，项目页 / 引擎卡 / 会话卡片
   都用它。`manual|smart|off` 是数据值，不改；这里只改显示。 */
const APPROVAL_LABELS: Record<string, string> = {
  manual: "每次询问",
  smart: "自动放行（智能判断）",
  off: "全部放行",
};

const readConversationPrefs = (name: string): ConversationPrefs => {
  try {
    const raw = window.localStorage.getItem(CONVERSATION_PREFS_KEY);
    if (!raw) return {};
    const prefs = JSON.parse(raw) as Record<string, ConversationPrefs>;
    const item = prefs?.[name] || {};
    return { active: typeof item.active === "string" && item.active.length > 0 ? item.active : null };
  } catch {
    return {};
  }
};

const writeConversationPrefs = (name: string, pref: ConversationPrefs) => {
  try {
    const raw = window.localStorage.getItem(CONVERSATION_PREFS_KEY);
    const prefs = raw ? JSON.parse(raw) as Record<string, ConversationPrefs> : {};
    prefs[name] = { ...(prefs[name] || {}), ...pref };
    window.localStorage.setItem(CONVERSATION_PREFS_KEY, JSON.stringify(prefs));
  } catch {
    // localStorage can be disabled in private/test contexts; the UI remains usable.
  }
};

const isEndedSession = (session: Session | undefined) => (
  session?.ended_at !== null && session?.ended_at !== undefined
);
const isRuntimeSession = (session: Session | undefined) => (
  Boolean(session?.runtime_recovered || session?.runtime_buffer_bytes !== undefined)
);
const tabTitle = (session: Session) => session.title || (session.preview ? session.preview.slice(0, 24) : "未命名对话");
const preferredActive = (sessions: Session[], prefs: ConversationPrefs) => {
  if (prefs.active && sessions.some((item) => item.id === prefs.active)) return prefs.active;
  return sessions.find(isRuntimeSession)?.id ?? sessions.find((item) => !isEndedSession(item))?.id ?? sessions[0]?.id ?? "__new__";
};
const commandFor = (name: string, sessionId?: string | null, model?: string | null, provider?: string | null) => {
  const base = `cd ${DASHBOARD_DIR} && hermes -p ${name} chat`;
  if (sessionId) return `${base} --resume ${sessionId}`;
  if (!model) return base;
  return `${base} --model ${model}${provider ? ` --provider ${provider}` : ""}`;
};
const compactNumber = (value?: number | null) => {
  const n = Number(value || 0);
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (Math.abs(n) >= 1_000) return `${Math.round(n / 1_000)}K`;
  return String(n);
};
const HEALTH_COPY: Record<SessionHealth["level"], { label: string; title: string; body: string }> = {
  ok: { label: "正常", title: "Session 健康正常", body: "当前估算上下文占用低于模型窗口的 50%，可继续。" },
  watch: { label: "观察", title: "上下文开始变大", body: "建议阶段总结后继续，避免工具输出继续堆积。" },
  bloated: { label: "偏肥", title: "上下文偏大", body: "建议让当前终端输出精简 handoff，再新开 session 继续。" },
  critical: { label: "过肥", title: "此 session 已过肥", body: "建议生成 handoff 后新开 session；继续打开仍允许，但会频繁压缩。" },
  unknown: { label: "未知", title: "健康信息不可用", body: "无法读取完整会话统计；仍可复制命令手动打开。" },
};
const handoffPromptFor = (agent: string, sessionId: string) => (
  `请基于当前会话输出一份精简 handoff，用于新开 Hermes session 继续工作。要求：\n` +
  `- 面向 agent/profile: ${agent}\n` +
  `- 当前 session_id: ${sessionId}\n` +
  `- 只保留目标、当前状态、关键决策、待办、阻塞、必须保留的文件路径/命令/ID。\n` +
  `- 不要粘贴大段 tool output、patch diff、read_file 全文或日志全文；只写摘要和路径。\n` +
  `- 明确下一步可以在新 session 直接执行什么。\n` +
  `- 输出控制在 1200 字以内。`
);

export function Conversation({
  name,
  node,
  parentLabel,
  onDetail,
  theme,
  onToggleTheme,
  refreshKey = 0,
}: {
  name: string;
  node: OrgNode;
  parentLabel: string | null;
  onDetail: () => void;
  theme: "dark" | "light";
  onToggleTheme: () => void;
  refreshKey?: number;
  visible?: boolean;
}) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [sessionErr, setSessionErr] = useState<string | null>(null);
  const [lever, setLever] = useState<Lever | null>(null);
  const [leverErr, setLeverErr] = useState<string | null>(null);
  const [showAllSessions, setShowAllSessions] = useState(false);
  const [sessionHealth, setSessionHealth] = useState<SessionHealth | null>(null);
  const [healthErr, setHealthErr] = useState<string | null>(null);
  const [loadingHealth, setLoadingHealth] = useState(false);
  const [terminalApp, setTerminalApp] = useState<string>("cmux");
  const [availableTerminalApps, setAvailableTerminalApps] = useState<DashboardConfig["available_terminal_apps"]>([]);
  const sessionPickerRef = useRef<HTMLDivElement | null>(null);

  const activeSession = useMemo(
    () => (active && active !== "__new__" ? sessions.find((item) => item.id === active) : undefined),
    [active, sessions],
  );
  const visibleTabSessions = useMemo(() => {
    const selected = new Map<string, Session>();
    for (const session of sessions) {
      if (session.id === active || isRuntimeSession(session)) selected.set(session.id, session);
    }
    return [...selected.values()];
  }, [active, sessions]);
  const effectiveModel = typeof lever?.model === "object" ? lever.model?.default || null : lever?.model || node.model || null;
  const effectiveProvider = lever?.known_models.find((item) => item.default === effectiveModel)?.provider || null;
  const selectedCommand = commandFor(name, activeSession?.id ?? null);
  const newCommand = commandFor(name, null, effectiveModel, effectiveProvider);

  const persistActive = (nextActive: string | null) => {
    writeConversationPrefs(name, { active: nextActive === "__new__" ? null : nextActive });
  };
  const chooseFallbackActive = (excludeId?: string, nextSessions = sessions) => (
    nextSessions.find((item) => item.id !== excludeId && isRuntimeSession(item))?.id ??
    nextSessions.find((item) => item.id !== excludeId && !isEndedSession(item))?.id ??
    nextSessions.find((item) => item.id !== excludeId)?.id ??
    "__new__"
  );

  const loadLever = () => apiGet<Lever>(`/api/agent/${encodeURIComponent(name)}/levers`)
    .then((value) => { setLeverErr(null); setLever(value); })
    .catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      setLever(null);
      setLeverErr(message);
    });

  const loadSessions = (preserveActive = true) => {
    setLoadingSessions(true);
    return fetchSessions(name)
      .then((r) => {
        const nextSessions = r.sessions || [];
        setSessions(nextSessions);
        setNote(r.note ?? null);
        setSessionErr(null);
        setActive((current) => {
          if (preserveActive && current && (current === "__new__" || nextSessions.some((item) => item.id === current))) return current;
          const nextActive = preferredActive(nextSessions, readConversationPrefs(name));
          persistActive(nextActive);
          return nextActive;
        });
      })
      .catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        setNote(message);
        setSessionErr(message);
        setActive("__new__");
      })
      .finally(() => setLoadingSessions(false));
  };

  const reloadAll = () => {
    void loadLever();
    void loadSessions(true);
  };

  useEffect(() => {
    const prefs = readConversationPrefs(name);
    setSessions([]);
    setActive(null);
    setNote(null);
    setSessionErr(null);
    setLever(null);
    setLeverErr(null);
    void loadLever();
    setLoadingSessions(true);
    fetchSessions(name)
      .then((r) => {
        const nextSessions = r.sessions || [];
        const nextActive = preferredActive(nextSessions, prefs);
        setSessions(nextSessions);
        setNote(r.note ?? null);
        setSessionErr(null);
        setActive(nextActive);
        persistActive(nextActive);
      })
      .catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        setNote(message);
        setSessionErr(message);
        setActive("__new__");
      })
      .finally(() => setLoadingSessions(false));
  }, [name]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!refreshKey) return;
    reloadAll();
  }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (active == null) return;
    persistActive(active);
  }, [name, active]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!activeSession?.id) {
      setSessionHealth(null);
      setHealthErr(null);
      setLoadingHealth(false);
      return;
    }
    let cancelled = false;
    setLoadingHealth(true);
    fetchSessionHealth(name, activeSession.id)
      .then((value) => {
        if (cancelled) return;
        setSessionHealth(value);
        setHealthErr(null);
      })
      .catch((error) => {
        if (cancelled) return;
        setSessionHealth(null);
        setHealthErr(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (!cancelled) setLoadingHealth(false);
      });
    return () => { cancelled = true; };
  }, [name, activeSession?.id]);

  useEffect(() => {
    if (!showAllSessions) return;
    const handler = (e: MouseEvent) => {
      if (sessionPickerRef.current && !sessionPickerRef.current.contains(e.target as Node)) {
        setShowAllSessions(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showAllSessions]);

  useEffect(() => {
    getDashboardConfig().then(
      (cfg) => { setTerminalApp(cfg.terminal_app || "cmux"); setAvailableTerminalApps(cfg.available_terminal_apps || []); },
      () => { /* keep defaults */ },
    );
  }, []);

  const setTerminalPref = async (app: string) => {
    try {
      await setDashboardConfig({ terminal_app: app });
      setTerminalApp(app);
      toast(`终端已切换为 ${app}，下次启动生效`, "ok");
    } catch (error) {
      toast(`切换失败: ${error instanceof Error ? error.message : error}`, "bad");
    }
  };

  const copyCommand = async (command: string) => {
    try {
      await navigator.clipboard.writeText(command);
      toast("启动命令已复制", "ok");
    } catch (error) {
      toast(`复制失败：${error instanceof Error ? error.message : error}`, "bad");
    }
  };

  const copyHandoffPrompt = async () => {
    if (!activeSession?.id) return;
    try {
      await navigator.clipboard.writeText(handoffPromptFor(name, activeSession.id));
      toast("handoff 提示词已复制", "ok");
    } catch (error) {
      toast(`复制失败：${error instanceof Error ? error.message : error}`, "bad");
    }
  };

  const launchTerminal = async (sessionId?: string | null) => {
    try {
      await openTerminal(name, sessionId || null);
      toast(sessionId ? "已打开真实终端继续该会话" : "已打开真实终端新建对话", "ok");
      setTimeout(() => { void loadSessions(true); }, 1200);
    } catch (error) {
      toast(`打开终端失败，可复制命令手动执行：${error instanceof Error ? error.message : error}`, "bad");
    }
  };

  const startNew = () => {
    setActive("__new__");
    persistActive("__new__");
  };

  const closeCurrentView = () => {
    if (!active || active === "__new__") return;
    const nextActive = chooseFallbackActive(active);
    setActive(nextActive);
    persistActive(nextActive);
    toast("已关闭当前会话视图；历史记录保留", "ok");
  };

  const deleteConversation = async (session: Session) => {
    const ok = await confirmAsync(`删除对话「${tabTitle(session)}」？这会永久删除该会话记录。`, {
      okLabel: "删除对话",
      danger: true,
    });
    if (!ok) return;
    try {
      await apiDelete(`/api/session/${encodeURIComponent(name)}/${encodeURIComponent(session.id)}`);
      const remaining = sessions.filter((item) => item.id !== session.id);
      const nextActive = active === session.id ? chooseFallbackActive(session.id, remaining) : active;
      setSessions(remaining);
      if (active === session.id) setActive(nextActive);
      persistActive(nextActive);
      toast("对话已删除", "ok");
    } catch (error) {
      toast(`删除失败：${error instanceof Error ? error.message : error}`, "bad");
    }
  };

  const leverLoaded = lever !== null;
  const curModel = leverLoaded ? (typeof lever.model === "object" ? lever.model?.default : lever.model) || "" : (node.model || "");
  const knownModels = lever?.known_models || [];
  const modelSelVal = knownModels.some((m) => m.default === curModel) ? curModel : "";
  const reasoningSelVal = lever ? (!lever.reasoning_own ? "__inherit__" : lever.reasoning_effort || "medium") : "medium";
  // 模型感知的推理强度选项：只展示当前模型支持的级别
  const availableReasoningLevels: string[] = lever?.model_reasoning_levels || [];
  const reasoningOptions = availableReasoningLevels.length > 0
    ? availableReasoningLevels
    : Object.keys(REASONING_LABELS);
  const modelFastMode = lever?.model_fast_mode || false;
  const fastSelVal = lever?.fast_mode ? "on" : "off";
  const approvalsSelVal = lever?.approvals_mode || "manual";

  const setModel = async (val: string) => {
    const selected = knownModels.find((m) => m.default === val);
    if (!selected) {
      toast("请选择模型白名单里的具体模型", "bad");
      return;
    }
    try {
      await apiPost<LeverUpdate>(`/api/agent/${encodeURIComponent(name)}/levers`, { model: selected });
      await loadLever();
      toast("默认模型已更新；真实终端会按 Hermes 当前配置启动", "ok");
    } catch (error) {
      toast(`设模型失败：${error instanceof Error ? error.message : error}`, "bad");
    }
  };

  const setReasoning = async (val: string) => {
    try {
      const updated = await apiPost<LeverUpdate>(
        `/api/agent/${encodeURIComponent(name)}/levers`,
        { reasoning_effort: val },
      );
      const effort = updated.effective_reasoning_effort || "medium";
      await loadLever();
      toast(`默认推理强度已切为${REASONING_LABELS[effort] || effort}；真实终端会按 Hermes 当前配置启动`, "ok");
    } catch (error) {
      toast(`设推理强度失败：${error instanceof Error ? error.message : error}`, "bad");
    }
  };

  const setFastMode = async (val: string) => {
    try {
      const updated = await apiPost<LeverUpdate>(
        `/api/agent/${encodeURIComponent(name)}/levers`,
        { fast_mode: val === "on" },
      );
      await loadLever();
      toast(`极速模式已${updated.effective_fast_mode ? "开启" : "关闭"}；新终端立即按此配置启动`, "ok");
    } catch (error) {
      toast(`设置极速模式失败：${error instanceof Error ? error.message : error}`, "bad");
    }
  };

  const setApprovals = async (val: string) => {
    try {
      if (val === "off" && lever?.approvals_mode !== "off") {
        const ok = await confirmAsync("把这个 agent 的危险命令审批切到「全部放行」？之后后续命令不会再逐条询问。", {
          danger: true,
          okLabel: "切到全部放行",
        });
        if (!ok) return;
      }
      await apiPost<LeverUpdate>(
        `/api/agent/${encodeURIComponent(name)}/levers`,
        { approvals_mode: val },
      );
      await loadLever();
      toast(`危险命令审批已切为${APPROVAL_LABELS[val] || val}；后续终端命令审批按新策略执行`, "ok");
    } catch (error) {
      toast(`设危险命令审批失败：${error instanceof Error ? error.message : error}`, "bad");
    }
  };

  return (
    <section className="agent-chat">
      {/* AD-98：这里原来有一个返回箭头（回 Agent 浏览）。全站没有「上一级」，
          退出这一页走侧栏，所以整块删掉。 */}
      <header className="agent-chat-head">
        <h2>{dname(node)}</h2>
        <div className="agent-chat-meta">
          <span className={`agent-chat-status ${node.gateway === "running" ? "is-online" : ""}`} />
          {node.main_twin ? (
            <span>{curModel || "—"} · 化身</span>
          ) : (
            <select
              className="agent-chat-model"
              value={modelSelVal}
              onChange={(e) => { void setModel(e.target.value); }}
              title={(() => {
                const sel = knownModels.find(m => m.default === modelSelVal);
                return sel?.description
                  ? `${sel.default} — ${sel.description}\n推理: ${(sel.reasoning_levels || []).join(", ")}${sel.fast_mode ? "\n快速模式: 可用" : ""}`
                  : "保存为该 agent 的默认模型；真实终端按 Hermes 当前配置启动";
              })()}
            >
              {!leverLoaded && <option value="" disabled>{leverErr ? "模型加载失败" : "模型加载中…"}</option>}
              {leverLoaded && !modelSelVal && <option value="" disabled>{curModel ? `${curModel}（未列入候选）` : "未配置模型"}</option>}
              {knownModels.map((m) => (
                <option key={m.default} value={m.default} title={
                  (m.tier || m.family) ? `${m.family ? m.family + " · " : ""}${m.tier || ""}: ${m.reasoning_levels?.join(", ") || "未知"}` : ""
                }>
                  {m.default}{m.family ? ` (${m.family} ${m.tier || ""})` : ""}
                </option>
              ))}
            </select>
          )}
          {leverErr && <span title={leverErr} style={{ color: "var(--danger)" }}>· 运行设置加载失败</span>}
          {!node.main_twin && (
            <select
              className="agent-chat-model agent-chat-reasoning"
              value={reasoningSelVal}
              onChange={(e) => { void setReasoning(e.target.value); }}
              title={`推理强度属于当前 agent；当前模型支持: ${reasoningOptions.join(", ")}`}
            >
              <option value="__inherit__">推理 · 继承{!lever?.reasoning_own ? ` · ${REASONING_LABELS[lever?.reasoning_effort || "medium"]}` : ""}</option>
              {reasoningOptions.map((value) => (
                <option key={value} value={value}>推理 · {REASONING_LABELS[value] || value}</option>
              ))}
            </select>
          )}
          {!node.main_twin && modelFastMode && (
            <select
              className="agent-chat-model agent-chat-reasoning"
              value={fastSelVal}
              onChange={(e) => { void setFastMode(e.target.value); }}
              title="Codex 极速模式约 1.5×；GPT-5.6/5.5 消耗约 2.5× ChatGPT credits"
            >
              <option value="on">极速 · 开</option>
              <option value="off">极速 · 关</option>
            </select>
          )}
          {!node.main_twin && (
            <select
              className="agent-chat-model agent-chat-approvals"
              value={approvalsSelVal}
              onChange={(e) => { void setApprovals(e.target.value); }}
              title="危险命令审批：保存该 agent 的策略；后续终端命令审批按新策略执行"
            >
              <option value="manual">危险命令审批 · {APPROVAL_LABELS.manual}</option>
              <option value="smart">危险命令审批 · {APPROVAL_LABELS.smart}</option>
              <option value="off">危险命令审批 · {APPROVAL_LABELS.off}</option>
            </select>
          )}
          {availableTerminalApps.length > 1 && (
            <select
              className="agent-chat-model agent-chat-reasoning"
              value={terminalApp}
              onChange={(e) => { void setTerminalPref(e.target.value); }}
              title={`终端应用：当前用 ${terminalApp} 打开 hermes 对话`}
            >
              {availableTerminalApps.map((a) => (
                <option key={a.name} value={a.name}>终端 · {a.label}</option>
              ))}
            </select>
          )}
          {parentLabel && <span>· 上级 {parentLabel}</span>}
        </div>
        <nav className="agent-chat-segments">
          <div className="agent-session-picker" ref={sessionPickerRef}>
            <button
              type="button"
              onClick={() => setShowAllSessions((v) => !v)}
              title="浏览全部历史会话"
            >
              会话 <ChevronDown className={`size-3.5 transition-transform ${showAllSessions ? "rotate-180" : ""}`} />
            </button>
            {showAllSessions && (
              <div className="agent-session-dropdown">
                <div className="agent-session-dropdown-head">
                  <History className="size-3.5" /> 全部会话 ({sessions.length})
                </div>
                <div className="agent-session-dropdown-list">
                  {sessions.map((s) => (
                    <button
                      key={s.id}
                      type="button"
                      className={`agent-session-dropdown-item ${active === s.id ? "is-active" : ""} ${isEndedSession(s) ? "is-ended" : ""}`}
                      onClick={() => {
                        setActive(s.id);
                        persistActive(s.id);
                        setShowAllSessions(false);
                      }}
                      title={s.title || s.preview || s.id}
                    >
                      <span className="agent-session-dropdown-title">{tabTitle(s)}</span>
                      <span className="agent-session-dropdown-meta">
                        {isEndedSession(s) ? "已结束" : isRuntimeSession(s) ? "运行中" : ""}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
          <button onClick={onDetail}>详情</button>
          <button onClick={onToggleTheme} title="切换全局亮/暗主题">
            {theme === "dark" ? <Sun /> : <Moon />}
          </button>
          <button onClick={reloadAll} title="重新载入"><RotateCw /></button>
        </nav>
      </header>

      <div className="agent-session-tabs">
        {visibleTabSessions.map((session) => (
          <div key={session.id} className={`agent-session-tab ${active === session.id ? "is-active" : ""}`}>
            <button
              type="button"
              className="agent-session-tab-label"
              onClick={() => {
                setActive(session.id);
                persistActive(session.id);
              }}
              title={tabTitle(session)}
            >
              {tabTitle(session)}
            </button>
            {active === session.id && (
              /* X 只出现在激活 tab 上:closeCurrentView 只能关"当前"视图,挂在别的 tab 上会关错对象 */
              <button
                type="button"
                className="agent-session-close"
                onClick={closeCurrentView}
                title="关闭当前会话视图；历史记录保留"
                aria-label={`关闭当前会话视图 ${tabTitle(session)}`}
              >
                <X />
              </button>
            )}
          </div>
        ))}
        <div className={`agent-session-tab ${active === "__new__" ? "is-active" : ""}`}>
          <button type="button" className="agent-session-tab-label" onClick={startNew} title="新建真实终端对话">
            <Plus className="size-3.5" />
            新对话
          </button>
        </div>
        {activeSession && (
          <button
            type="button"
            className="agent-session-delete-history"
            onClick={() => { void deleteConversation(activeSession); }}
            title="永久删除当前会话历史记录"
          >
            <Trash2 className="size-3.5" />
            删除历史
          </button>
        )}
        {sessionErr && (
          <span className="agent-session-error" title={sessionErr}>
            会话列表加载失败：{sessionErr}
          </span>
        )}
      </div>

      <div className="agent-terminal-stage agent-terminal-launcher-stage">
        <div className="real-terminal-launcher">
          <div className="real-terminal-main">
            <div className="real-terminal-kicker">{t("legacy.terminal.kicker")}</div>
            <h3>{activeSession ? tabTitle(activeSession) : "新建对话"}</h3>
            <p>
              网页只保留 agent 与 session 选择；实时输入输出交给 macOS Terminal 中的 Hermes CLI。
            </p>
            <div className="real-terminal-meta">
              <span>Agent: {name}</span>
              {activeSession ? <span>Session: {activeSession.id}</span> : <span>Session: 新建</span>}
              {activeSession && <span>{isEndedSession(activeSession) ? "历史" : isRuntimeSession(activeSession) ? "运行中" : "可续接"}</span>}
            </div>
            {activeSession && (
              <div className={`session-governor-card is-${sessionHealth?.level || (healthErr ? "unknown" : "ok")}`}>
                <div className="session-governor-head">
                  <span className="session-governor-badge">
                    <AlertTriangle className="size-3.5" />
                    {loadingHealth ? "检测中" : HEALTH_COPY[sessionHealth?.level || (healthErr ? "unknown" : "ok")].label}
                  </span>
                  <strong>{loadingHealth ? "正在检查 session 上下文" : HEALTH_COPY[sessionHealth?.level || (healthErr ? "unknown" : "ok")].title}</strong>
                </div>
                <p>{loadingHealth ? "Session Governor V2 正按模型窗口做只读统计，不会修改历史。" : HEALTH_COPY[sessionHealth?.level || (healthErr ? "unknown" : "ok")].body}</p>
                {sessionHealth?.stats && (
                  <div className="session-governor-metrics">
                    <span>消息 {sessionHealth.stats.message_count}</span>
                    <span>上下文 ≈{compactNumber(sessionHealth.stats.estimated_tokens)}/{compactNumber(sessionHealth.stats.context_window)} tokens ({sessionHealth.stats.context_percent}%)</span>
                    <span>工具 ≈{compactNumber(sessionHealth.stats.tool_tokens)} tokens</span>
                    <span>tail ≈{compactNumber(sessionHealth.stats.recent_tail_tokens)} tokens</span>
                    <span>父链 {sessionHealth.stats.parent_chain_depth}</span>
                  </div>
                )}
                {(sessionHealth?.reasons?.length || healthErr) && (
                  <ul className="session-governor-reasons">
                    {healthErr ? (
                      <li>健康信息读取失败：{healthErr}</li>
                    ) : (
                      sessionHealth?.reasons.slice(0, 3).map((reason) => <li key={reason}>{reason}</li>)
                    )}
                  </ul>
                )}
                {sessionHealth && sessionHealth.level !== "ok" && (
                  <button type="button" className="session-governor-copy" onClick={() => { void copyHandoffPrompt(); }}>
                    <Clipboard className="size-3.5" />
                    复制 handoff 提示词
                  </button>
                )}
              </div>
            )}
            <pre className="real-terminal-command">{activeSession ? selectedCommand : newCommand}</pre>
            <div className="real-terminal-actions">
              <button type="button" className="real-terminal-primary" onClick={() => { void launchTerminal(activeSession?.id ?? null); }}>
                <ExternalLink className="size-4" />
                {activeSession ? "打开真实终端继续当前 session" : "新建真实终端对话"}
              </button>
              <button type="button" onClick={() => { void copyCommand(activeSession ? selectedCommand : newCommand); }}>
                <Clipboard className="size-4" />
                复制启动命令
              </button>
            </div>
          </div>

          <aside className="real-terminal-side">
            <div className="real-terminal-side-head">
              <span>最近会话</span>
              <button type="button" onClick={() => { void loadSessions(true); }} title="刷新会话">
                <RotateCw className="size-3.5" />
              </button>
            </div>
            <div className="real-terminal-session-list">
              {loadingSessions ? (
                <div className="real-terminal-empty">正在加载会话…</div>
              ) : sessions.length ? (
                sessions.slice(0, 10).map((session) => (
                  <button
                    key={session.id}
                    type="button"
                    className={active === session.id ? "is-active" : ""}
                    onClick={() => {
                      setActive(session.id);
                      persistActive(session.id);
                    }}
                  >
                    <span>{tabTitle(session)}</span>
                    <small>{isEndedSession(session) ? "已结束" : isRuntimeSession(session) ? "运行中" : session.id}</small>
                  </button>
                ))
              ) : (
                <div className="real-terminal-empty">{note || "暂无会话"}</div>
              )}
            </div>
          </aside>
        </div>
      </div>

      <div className="agent-session-note" style={{ color: cream(55) }}>
        正式入口不再自动启动浏览器内嵌 PTY；需要实时对话时请打开真实终端，或复制命令手动执行。
      </div>
    </section>
  );
}
