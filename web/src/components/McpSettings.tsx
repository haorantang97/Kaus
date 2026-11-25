import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronRight, Info, LogIn, LogOut, Lock, Pencil, Plug, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useLocale } from "../i18n";
import { bindMcp, checkMcp, fetchMcp, fetchMcpList, importMcp, loginMcp, removeMcp, signOutMcp, unbindMcp,
  type McpDetail, type McpGroup, type McpItem, type McpStatus } from "../lib/mcpApi";
import { EngineIcon } from "./EngineIcon";
import { McpEditDialog, McpEditForm } from "./McpEditDialog";
import { McpToolSwitches } from "./McpToolSwitches";
import { Button, Modal, Pill, Spinner, confirmAsync, cream, undoToast } from "./ui";

const REASONS = ["agent_owned", "agent_self", "agent_tools", "not_isolable", "user_marked", "memory_not_isolable"] as const;
type Reason = typeof REASONS[number];
const reason = (value?: string): Reason => REASONS.includes(value as Reason) ? value as Reason : "agent_owned";
const GROUPS: readonly McpGroup[] = ["available", "attention", "bound", "notImported"];
const STATUS_TONE: Record<McpStatus, "success" | "gold" | "danger" | "muted"> = {
  ok: "success", auth_required: "gold", failed: "danger", missing: "danger", unchecked: "muted",
};

/* MCP 管理（2026-11-11，docs/product/mcp-management-design-20261111.md）：设置里与「Agent」并列。
   技能树管分给哪些 Project；这里管 MCP 本身——连接状态、工具开关、是否绑定在某个 Agent 上。 */
export function McpSettings() {
  const { t } = useLocale();
  const [items, setItems] = useState<McpItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  // `/config?section=mcp&mcp=<id>` (from the skill tree) opens that MCP directly.
  const [open, setOpen] = useState<string | null>(() => new URLSearchParams(window.location.search).get("mcp"));
  const [adding, setAdding] = useState(false);
  const autoChecked = useRef(false);

  const check = useCallback(async (ids?: string[]) => {
    setChecking(true); setError(null);
    try { setItems((await checkMcp(ids)).items); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setChecking(false); }
  }, []);
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const list = (await fetchMcpList(signal)).items;
      setItems(list);
      // First visit: check what was never checked, so the page shows real connection states.
      const unchecked = list.filter(i => i.status === "unchecked" && i.group !== "notImported").map(i => i.id);
      if (!autoChecked.current && unchecked.length) { autoChecked.current = true; void check(unchecked); }
    } catch (failure) { if (!signal?.aborted) setError(failure instanceof Error ? failure.message : String(failure)); }
  }, [check]);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const row = (item: McpItem) => <button key={item.id} type="button" className="kaus-mcp-row" onClick={() => setOpen(item.id)}
    aria-label={t("mcp.detailsOf", { name: item.name })}>
    <span className="kaus-mcp-icon">{item.origin === "agent" ? <EngineIcon engine={item.agentKey} size={18} /> : <Plug size={17} />}</span>
    <span className="kaus-mcp-main">
      <strong>{item.name}</strong>
      <small>{item.origin === "agent" ? item.agentLabel : t("mcp.origin.kaus")}{item.target ? ` · ${item.target}` : ""}</small>
    </span>
    {item.group === "bound" && item.boundReason && <span className="kaus-mcp-reason">{t(`mcp.reason.${reason(item.boundReason)}`)}</span>}
    {item.toolCount !== null && <small className="kaus-mcp-tools">{t("mcpTools.count", { count: item.toolCount - item.disabledTools.length })}</small>}
    {item.group !== "notImported" && <Pill tone={STATUS_TONE[item.status]}>{t(`mcp.status.${item.status}`)}</Pill>}
    <ChevronRight size={15} aria-hidden="true" style={{ color: cream(45) }} />
  </button>;

  return <div className="kaus-settings-section">
    <div className="kaus-settings-heading"><h2 className="kaus-settings-page-title">MCP</h2>
      <div className="flex gap-2">
        <Button disabled={checking || !items?.length} onClick={() => void check()} aria-label={t("mcp.check")} title={t("mcp.check")}>
          <RefreshCw size={15} className={checking ? "animate-spin" : undefined} /></Button>
        <Button onClick={() => setAdding(true)}><Plus size={15} />{t("mcp.add")}</Button>
      </div>
    </div>
    {error && <p role="alert" className="kaus-group-error">{error}</p>}
    {!items ? <p className="flex items-center gap-2 text-xs" style={{ color: cream(50) }}><Spinner />{t("ui.loading")}</p>
      : !items.length ? <p className="kaus-settings-empty">{t("mcp.empty")}</p>
      : GROUPS.map(group => {
        const rows = items.filter(item => item.group === group);
        if (!rows.length) return null;
        return <section key={group} className="kaus-mcp-group" aria-label={t(`mcp.group.${group}`)}>
          <h3>{t(`mcp.group.${group}`)}<span>{rows.length}</span>
            {group === "bound" && <span className="kaus-mcp-hint" role="img" aria-label={t("mcp.boundHint")} title={t("mcp.boundHint")}><Info size={13} /></span>}
            {group === "notImported" && <span className="kaus-mcp-hint" role="img" aria-label={t("mcp.notImportedHint")} title={t("mcp.notImportedHint")}><Info size={13} /></span>}
          </h3>
          {rows.map(row)}
        </section>;
      })}
    {open && <McpDetailDialog id={open} onClose={() => setOpen(null)} onChanged={() => void load()} onEdited={() => void load()} />}
    {adding && <McpEditDialog onClose={() => setAdding(false)} onSaved={saved => { setAdding(false); void load(); void check([saved.id]); setOpen(saved.id); }} />}
  </div>;
}

function McpDetailDialog({ id, onClose, onChanged, onEdited }: { id: string; onClose: () => void; onChanged: () => void; onEdited: () => void }) {
  const { t } = useLocale();
  const [detail, setDetail] = useState<McpDetail | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toolsKey, setToolsKey] = useState(0);

  const load = useCallback(async (signal?: AbortSignal) => {
    try { setDetail(await fetchMcp(id, signal)); }
    catch (failure) { if (!signal?.aborted) setError(failure instanceof Error ? failure.message : String(failure)); }
  }, [id]);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const act = async (work: () => Promise<unknown>, close = false) => {
    setBusy(true); setError(null);
    try { await work(); onChanged(); if (close) onClose(); else await load(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(false); }
  };
  const recheck = () => act(async () => { await checkMcp([id]); setToolsKey(k => k + 1); });
  // Sign-in happens in the browser; the detail follows until Kaus holds the token (or the window closes).
  const [signingIn, setSigningIn] = useState(false);
  const signIn = async () => {
    setError(null);
    try {
      const started = await loginMcp(id);
      if (started.authorizationUrl) window.open(started.authorizationUrl, "_blank", "noopener");
      setSigningIn(true);
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
  };
  useEffect(() => {
    if (!signingIn) return;
    let stopped = false;
    const deadline = Date.now() + 10 * 60_000;
    const poll = async () => {
      if (stopped) return;
      try {
        const next = await fetchMcp(id);
        if (next.signedIn && next.status !== "unchecked") { setDetail(next); setSigningIn(false); setToolsKey(k => k + 1); onChanged(); return; }
      } catch { /* keep waiting */ }
      if (Date.now() < deadline) window.setTimeout(() => void poll(), 2000); else setSigningIn(false);
    };
    void poll();
    return () => { stopped = true; };
  }, [signingIn, id, onChanged]);

  if (!detail) return <Modal onClose={onClose} width={560}>
    <p className="flex items-center gap-2 py-6 text-xs" style={{ color: cream(50) }}>{error ?? <><Spinner />{t("ui.loading")}</>}</p>
  </Modal>;

  const agent = detail.origin === "agent";
  const removable = !agent || (detail.group !== "notImported" && detail.group !== "bound");
  const definition = detail.definition;
  const keys = definition?.transport === "stdio" ? definition.env ?? [] : definition?.headers ?? [];
  const bind = async () => {
    if (!await confirmAsync(t("mcp.bindConfirm", { agent: detail.agentLabel }), { title: t("mcp.bindTitle", { agent: detail.agentLabel }), okLabel: t("mcp.bind") })) return;
    await act(() => bindMcp(id));
  };
  const remove = async () => {
    // An imported MCP only leaves the library (the Agent's own config stays), so it can be imported back:
    // 「撤销」 instead of asking. One added in Kaus loses its stored keys, so that one still asks.
    if (agent) {
      await act(() => removeMcp(id), true);
      undoToast(t("ui.deleted", { name: detail.name }), async () => { try { await importMcp(id); onChanged(); } catch { /* list shows the state */ } });
      return;
    }
    if (!await confirmAsync(t("mcp.removeConfirm"), { title: t("mcp.removeTitle"), okLabel: t("mcp.remove"), danger: true })) return;
    await act(() => removeMcp(id), true);
  };

  if (editing) return <Modal onClose={onClose} width={580}>
    <McpEditForm editing={detail} onClose={() => setEditing(false)}
      onSaved={() => { setEditing(false); onEdited(); void recheck(); }} />
  </Modal>;

  return <Modal onClose={onClose} width={580}>
    <div className="flex items-start gap-3">
      <span className="mt-0.5" style={{ color: cream(60) }}>{agent ? <EngineIcon engine={detail.agentKey} size={20} /> : <Plug size={19} />}</span>
      <div className="min-w-0 flex-1">
        <h3 className="kaus-modal-title">{detail.name}</h3>
        <p className="text-xs" style={{ color: cream(55) }}>{agent ? detail.agentLabel : t("mcp.origin.kaus")}{detail.description ? ` · ${detail.description}` : ""}</p>
      </div>
      {!agent && <button type="button" className="skill-tree-icon" disabled={busy} title={t("mcp.edit")} aria-label={t("mcp.edit")}
        onClick={() => setEditing(true)}><Pencil size={15} /></button>}
      {removable && <button type="button" className="skill-tree-icon" disabled={busy} title={t("mcp.remove")} aria-label={t("mcp.remove")}
        onClick={() => void remove()}><Trash2 size={15} /></button>}
    </div>
    {error && <p role="alert" className="mt-2 text-xs" style={{ color: "var(--danger)" }}>{error}</p>}

    {detail.group === "notImported" ? <div className="kaus-mcp-section">
      <div><Button variant="primary" disabled={busy} onClick={() => void act(() => importMcp(id))}>{t("mcp.import")}</Button></div>
    </div> : <div className="kaus-mcp-section">
      <h4>{t("mcp.section.status")}</h4>
      <div className="flex items-center gap-2">
        <Pill tone={STATUS_TONE[detail.status]}>{t(`mcp.status.${detail.status}`)}</Pill>
        <span className="flex-1" />
        {detail.status !== "missing" && <button type="button" className="skill-tree-icon" disabled={busy} title={t("mcp.recheck")} aria-label={t("mcp.recheck")}
          onClick={() => void recheck()}><RefreshCw size={14} /></button>}
      </div>
      {detail.error && <details className="mt-1.5 text-xs" style={{ color: cream(55) }}>
        <summary>{t("mcp.diagnostics")}</summary><pre className="kaus-mcp-diagnostics">{detail.error}</pre></details>}
    </div>}

    <div className="kaus-mcp-section">
      <h4>{t("mcp.section.connection")}</h4>
      {definition && <p className="text-xs" style={{ color: cream(62) }}>
        {t(`mcp.transport.${definition.transport === "stdio" ? "stdio" : "http"}`)} · <code className="break-all">{detail.target}</code></p>}
      {keys.length > 0 && <ul className="mt-1 text-xs" style={{ color: cream(55) }}>{keys.map(key => <li key={key} className="flex gap-2">
        <code>{key}</code>{!agent && <span>{definition?.valuesSet?.includes(key) ? t("mcp.valueSet") : t("mcp.valueMissing")}</span>}</li>)}</ul>}
      {detail.sourcePath && <p className="mt-1 break-all text-xs" style={{ color: cream(42) }}>{t("import.source")} · {detail.sourcePath}</p>}
      {/* MCP 统一登录（2026-11-12）：Kaus 登录一次，令牌给用到它的任何 Agent。 */}
      {detail.signedIn !== undefined && (detail.signedIn
        ? <div className="mt-2 flex items-center gap-2 text-xs" style={{ color: cream(62) }}>
            <span className="flex-1">{t("mcp.signedIn")}</span>
            <button type="button" className="skill-tree-icon" disabled={busy} title={t("mcp.signOut")} aria-label={t("mcp.signOut")}
              onClick={() => void act(() => signOutMcp(id))}><LogOut size={14} /></button>
          </div>
        : detail.status === "auth_required" && <div className="mt-2">
            <Button variant="primary" disabled={busy || signingIn} onClick={() => void signIn()}>
              {signingIn ? <Spinner /> : <LogIn size={14} />}{t("mcp.signIn")}</Button>
          </div>)}
    </div>

    {detail.group !== "notImported" && (detail.status === "ok" || detail.status === "unchecked") && <div className="kaus-mcp-section">
      <McpToolSwitches key={toolsKey} skillId={id} disabled={detail.disabledTools} onSnapshot={() => void act(async () => {})} />
    </div>}

    {agent && <div className="kaus-mcp-section">
      <h4>{t("mcp.section.binding")}</h4>
      <div className="flex items-center gap-2">
        <span className="flex flex-1 items-center gap-1.5 text-xs" style={{ color: cream(62) }}>
          {detail.group === "bound" ? <><Lock size={12} aria-hidden="true" />{t(`mcp.reason.${reason(detail.boundReason)}`)} · {
            detail.boundReason === "not_isolable" ? t("mcp.notIsolableExplain") : t("mcp.boundExplain", { agent: detail.agentLabel })}</>
            : t("mcp.sharedExplain")}
        </span>
        {detail.group === "bound"
          ? detail.boundReason !== "not_isolable" && <Button disabled={busy} onClick={() => void act(() => unbindMcp(id))}>{t("mcp.unbind")}</Button>
          : <Button disabled={busy} onClick={() => void bind()}>{t("mcp.bind")}</Button>}
      </div>
    </div>}

    {detail.group !== "notImported" && <div className="kaus-mcp-section">
      <h4>{t("mcp.section.assignment")}</h4>
      <p className="text-xs" style={{ color: cream(62) }}>{detail.projects.length ? detail.projects.map(p => p.name).join("、") : t("mcp.unassigned")}</p>
    </div>}

  </Modal>;
}
