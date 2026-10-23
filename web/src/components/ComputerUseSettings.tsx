import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ChevronRight, ExternalLink, LoaderCircle, Monitor, RefreshCw, X } from "lucide-react";
import { useLocale } from "../i18n";
import { sessionRequest } from "../lib/sessionApi";
import "./computer-use-settings.css";

type Permission = "screen-recording" | "accessibility";
type PermissionSetup = { host: string; label: string; permissions: Permission[] };
type Capability = { status: string; requirements: string[]; permissionSetup?: PermissionSetup };
type SetupStatus = "idle" | "checking" | "downloading" | "installing" | "needs_permission" | "ready" | "failed" | "unsupported";
type Snapshot = {
  enabled: boolean;
  setup: { status: SetupStatus; error?: string; version?: string; permissions?: Partial<Record<Permission, boolean>> };
  agents: { id: string; label: string; browser: Capability; desktop: Capability }[];
  fallbacks: (Capability & { provider: string; capability: string })[];
  pendingResources: string[];
  nativePermissionHosts?: string[];
};
const progressing = (status?: SetupStatus) => status === "checking" || status === "downloading" || status === "installing";

export function ComputerUseSettings({ refreshKey }: { refreshKey?: string | number }) {
  const { locale } = useLocale();
  const en = locale === "en";
  const [expanded, setExpanded] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const current = useRef<Snapshot | null>(null);
  const mounted = useRef(false);
  const read = useRef<AbortController | null>(null);
  const writing = useRef(false);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [actionFailed, setActionFailed] = useState(false);
  const [setupHost, setSetupHost] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [permissionError, setPermissionError] = useState<"request" | "open" | "reveal" | "verify" | null>(null);
  const [permissionPending, setPermissionPending] = useState(false);
  const [verified, setVerified] = useState(false);
  const enabled = snapshot?.enabled === true;
  const setupStatus = snapshot?.setup?.status ?? "idle";
  const allCapabilities = [...(snapshot?.agents.flatMap(agent => [agent.browser, agent.desktop]) ?? []), ...(snapshot?.fallbacks ?? [])];
  const cuaSetup = allCapabilities.find(value => value.permissionSetup?.host === "cua-driver")?.permissionSetup;
  const setup = enabled && (setupStatus === "needs_permission" || (setupStatus === "ready" && setupHost === "cua-driver"))
    ? cuaSetup ?? {host:"cua-driver",label:"CuaDriver",permissions:["screen-recording", "accessibility"] as Permission[]}
    : allCapabilities.find(value => value.permissionSetup?.host === setupHost)?.permissionSetup;

  const apply = useCallback((value: Snapshot) => {
    current.current = value; setSnapshot(value);
    if (!value.enabled || value.setup?.status !== "ready") setVerified(false);
  }, []);
  const load = useCallback(async (force = false, permissionsOnly = false) => {
    if (!mounted.current || read.current || writing.current) return;
    const controller = new AbortController(); read.current = controller;
    setLoading(true); setFailed(false);
    try {
      const value = await sessionRequest<Snapshot>(`/api/computer-use${permissionsOnly ? "/permissions" : force ? "?force=true" : ""}`, {signal:controller.signal});
      if (!controller.signal.aborted && mounted.current) apply(value);
    } catch { if (!controller.signal.aborted && mounted.current) setFailed(true); }
    finally {
      if (read.current === controller) read.current = null;
      if (!controller.signal.aborted && mounted.current) setLoading(false);
    }
  }, [apply]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; read.current?.abort(); read.current = null; };
  }, []);
  useEffect(() => {
    if (!expanded) return;
    setLoading(false);
    void load(current.current?.enabled === true, current.current !== null);
    const refresh = () => { if (current.current?.enabled && document.visibilityState !== "hidden") void load(false, true); };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      read.current?.abort(); read.current = null;
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [expanded, load, refreshKey]);
  useEffect(() => {
    if (!expanded || !enabled || (!progressing(setupStatus) && setupStatus !== "needs_permission") || loading || busy || failed) return;
    const timer = window.setTimeout(() => {
      if (document.visibilityState !== "hidden") void load(false, setupStatus === "needs_permission");
    }, progressing(setupStatus) ? 2000 : 3000);
    return () => window.clearTimeout(timer);
  }, [expanded, enabled, setupStatus, loading, busy, failed, load, snapshot]);

  async function update(action: "enable" | "disable" | "retry") {
    if (writing.current) return;
    writing.current = true; read.current?.abort(); read.current = null;
    setLoading(false); setBusy(true); setActionFailed(false); setFailed(false);
    try {
      const value = await sessionRequest<Snapshot>(action === "retry" ? "/api/computer-use/setup" : "/api/computer-use/enabled",
        action === "retry" ? {method:"POST"} : {method:"PUT",body:{enabled:action === "enable"}});
      if (mounted.current) { apply(value); setSetupHost(action === "enable" ? "cua-driver" : null); setPermissionError(null); setPermissionPending(false); setVerified(false); }
    } catch { if (mounted.current) setActionFailed(true); }
    finally { writing.current = false; if (mounted.current) setBusy(false); }
  }
  async function permissionAction(kind: string, path: string) {
    if (opening) return;
    setOpening(kind); setPermissionError(null); setPermissionPending(false);
    try {
      // One click registers the signed helper and opens the precise OS pane.
      // Pane navigation remains available if registration needs manual adding.
      if ((kind === "accessibility" || kind === "screen-recording") && setup?.host === "cua-driver") {
        await sessionRequest("/api/computer-use/permissions/request", {method:"POST"}).catch(() => undefined);
      }
      const result = await sessionRequest<{opened:boolean;pending?:boolean}>(path, {method:"POST"});
      if (mounted.current) setPermissionPending(result.opened === false);
      if (mounted.current && kind === "verify" && result.opened) setVerified(true);
    } catch { if (mounted.current) setPermissionError(kind === "request" ? "request" : kind === "reveal" ? "reveal" : kind === "verify" ? "verify" : "open"); }
    finally { if (mounted.current) { setOpening(null); if (kind !== "reveal") void load(false, true); } }
  }
  function status(value: Capability, native = false) {
    if (value.requirements.includes("connection_specific")) return en ? "By connection" : "依连接配置";
    if (value.status === "ready") return native ? (en ? "Native" : "原生") : (en ? "Ready" : "可用");
    if (value.status === "disabled") return en ? "Disabled" : "已关闭";
    if (value.status === "denied" || value.requirements.some(code => /permission|accessibility|screen.recording|authorization/.test(code))) return en ? "Permission needed" : "待授权";
    if (value.requirements.some(code => /install|missing|not_installed/.test(code))) return en ? "Setup needed" : "待配置";
    return en ? "Not verified" : "待检查";
  }
  function capabilityStatus(value: Capability, native = false) {
    const target = value.permissionSetup;
    if (!target) return status(value, native);
    return <button type="button" className="kaus-computer-use-permission-link"
      aria-label={`${target.label} ${en ? "permissions" : "授权"}`} aria-expanded={setup?.host === target.host}
      onClick={() => { setSetupHost(target.host); setPermissionError(null); }}>
      {status(value, native)}<ChevronRight size={12} aria-hidden />
    </button>;
  }
  const labels: Record<SetupStatus, string> = {
    idle: en ? "Not set up" : "未准备", checking: en ? "Checking" : "正在检查", downloading: en ? "Downloading" : "正在下载",
    installing: en ? "Installing" : "正在安装", needs_permission: en ? "Permission needed" : "待授权", ready: en ? "Ready" : "可用",
    failed: en ? "Setup incomplete" : "准备未完成", unsupported: en ? "Unavailable on this system" : "当前系统暂不可用",
  };
  const setupErrors: Record<string, [string,string]> = {
    download_failed:["下载未完成，请重试。","Download incomplete. Try again."],
    integrity_failed:["组件校验未通过，请重新下载。","Component verification failed. Retry the download."],
    signature_failed:["组件校验未通过，请重新下载。","Component verification failed. Retry the download."],
    runtime_check_failed:["组件暂时无法连接，请重试。","Unable to connect to the component. Try again."],
    install_failed:["安装未完成，请重试。","Installation incomplete. Try again."],
    setup_interrupted:["准备已暂停，可以重试。","Setup paused. You can retry."],
    macos_version_unsupported:["请更新 macOS 后重试。","Update macOS to continue."],
  };
  const setupError = snapshot?.setup?.error && setupErrors[snapshot.setup.error]?.[en ? 1 : 0];
  return <details className="kaus-settings-native kaus-computer-use" onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><span className="kaus-computer-use-title"><Monitor size={16} aria-hidden />{en ? "Computer use" : "电脑操作"}</span><ChevronRight size={15} aria-hidden /></summary>
    <div className="kaus-computer-use-toolbar">
      <label id="computer-use-enable-label">{en ? "Enable computer use" : "启用电脑操作"}</label>
      <button type="button" role="switch" className="kaus-computer-use-switch" aria-labelledby="computer-use-enable-label"
        aria-checked={enabled} disabled={!snapshot || busy} title={en ? "Turning off applies to future runs." : "关闭仅对后续运行生效。"}
        onClick={() => void update(enabled ? "disable" : "enable")}><span /></button>
      <button type="button" className="kaus-quota-refresh" disabled={loading || busy}
        aria-label={en ? "Refresh computer tools" : "刷新电脑工具"} title={en ? "Refresh" : "刷新"}
        onClick={() => void load(true)}><RefreshCw size={15} className={loading ? "animate-spin" : ""} aria-hidden /></button>
    </div>
    {!!snapshot?.nativePermissionHosts?.length && <p className="kaus-computer-use-caption">
      {en ? `${snapshot.nativePermissionHosts.join(", ")} native permissions are managed in the Agent.` : `${snapshot.nativePermissionHosts.join("、")} 的原生权限由 Agent 管理。`}
    </p>}
    {failed && <p className="kaus-computer-use-notice" role="status">{en ? "Unable to check. Try refreshing." : "暂时无法检查，请刷新"}</p>}
    {actionFailed && <p className="kaus-computer-use-notice" role="status">{en ? "Update incomplete. Try again." : "操作未完成，请重试"}</p>}
    {snapshot && enabled && <>
      <p className="kaus-computer-use-caption">{en ? "Agent-native tools are used first." : "优先使用 Agent 原生能力。"}</p>
      <div className="kaus-computer-use-setup">
        <span>CuaDriver</span><span className="kaus-computer-use-setup-status" role="status">
          {progressing(setupStatus) ? <LoaderCircle size={14} className="animate-spin" aria-hidden /> : setupStatus === "ready" ? <Check size={14} aria-hidden /> : null}{labels[setupStatus]}
        </span>
        {(setupStatus === "failed" || setupStatus === "idle") && <button type="button" className="kaus-computer-use-permission-link" disabled={busy}
          onClick={() => void update("retry")}><RefreshCw size={14} aria-hidden />{en ? "Retry" : "重试"}</button>}
        {setupStatus === "ready" && !setup && <button type="button" className="kaus-computer-use-permission-link" onClick={() => setSetupHost("cua-driver")}>{en ? "Permissions" : "授权"}<ChevronRight size={13} aria-hidden /></button>}
      </div>
      {setupError && (setupStatus === "failed" || setupStatus === "unsupported") && <p className="kaus-computer-use-notice">{setupError}</p>}
      {setup && <section className="kaus-computer-use-permission-panel" aria-label={en ? "Computer permissions" : "电脑操作授权"}>
        <div className="kaus-computer-use-permission-heading"><strong>{en ? "System permissions" : "系统授权"}</strong>
          {setupStatus !== "needs_permission" && <button type="button" className="kaus-quota-refresh" aria-label={en ? "Close permissions" : "关闭授权说明"} onClick={() => setSetupHost(null)}><X size={15} aria-hidden /></button>}
        </div>
        {setupStatus !== "ready" && <p>{en ? `Enable ${setup.label} in System Settings. Kaus checks when you return.` : `在系统设置中开启 ${setup.label}，返回后自动检查。`}</p>}
        {(setup.host === "cua-driver" ? ["accessibility", "screen-recording"] as Permission[] : setup.permissions).map(permission => {
          const label = permission === "screen-recording" ? (en ? "Screen & System Audio Recording" : "录屏与系统录音") : (en ? "Accessibility" : "辅助功能");
          const granted = setup.host === "cua-driver" && snapshot?.setup.permissions?.[permission] === true;
          const purpose = permission === "screen-recording" ? (en ? "Read the screen" : "读取屏幕") : (en ? "Click and type" : "点击与输入");
          return <div className="kaus-computer-use-permission-row" key={permission}><span className="kaus-computer-use-permission-name">{label}<small>{purpose}</small></span>
            {granted ? <span className="kaus-computer-use-granted"><Check size={14} aria-hidden />{en ? "Enabled" : "已开启"}</span> :
            <button type="button" className="kaus-computer-use-permission-link" disabled={opening !== null} aria-label={`${en ? "Open " : "打开"}${label}`}
              onClick={() => void permissionAction(permission, `/api/computer-use/permissions/${permission}`)}>{opening === permission ? <LoaderCircle size={13} className="animate-spin" aria-hidden /> : null}{en ? "Open Settings" : "去开启"}<ExternalLink size={13} aria-hidden /></button>}
          </div>;
        })}
        {setupStatus !== "ready" && <details className="kaus-computer-use-help"><summary>{en ? `Can't find ${setup.label}?` : `列表中没有 ${setup.label}？`}</summary>
          <p>{en ? `Click + in System Settings and add ${setup.label}.` : `点击系统设置中的「＋」，添加已定位的 ${setup.label}。`}</p>
          <button type="button" className="kaus-computer-use-permission-link" disabled={opening !== null} onClick={() => void permissionAction("reveal", `/api/computer-use/permissions/hosts/${encodeURIComponent(setup.host)}/reveal`)}>{en ? "Locate app" : "定位应用"}<ExternalLink size={13} aria-hidden /></button>
        </details>}
        <div className="kaus-computer-use-permission-footer">
          {setup.host === "cua-driver" && setupStatus === "ready" && <button type="button" className="kaus-computer-use-permission-link" disabled={opening !== null || verified} onClick={() => void permissionAction("verify", "/api/computer-use/permissions/verify")}>{verified ? <Check size={13} aria-hidden /> : <Monitor size={13} aria-hidden />}{verified ? (en ? "Screen verified" : "屏幕读取正常") : (en ? "Test screen access" : "测试屏幕读取")}</button>}
          <button type="button" className="kaus-computer-use-permission-link" disabled={loading || busy} onClick={() => void load(false, true)}><RefreshCw size={13} className={loading ? "animate-spin" : ""} aria-hidden />{en ? "Check again" : "重新检查"}</button>
        </div>
        {permissionError && <p className="kaus-computer-use-notice" role="status">{permissionError === "verify" ? (en ? "Screen check incomplete. Try again." : "屏幕读取未确认，请重试") : permissionError === "request" ? (en ? "Unable to request access. Try again." : "未能发起授权，请重试") : permissionError === "reveal" ? (en ? "Unable to reveal the tool" : "无法定位工具") : (en ? "Unable to open System Settings" : "无法打开系统设置")}</p>}
        {permissionPending && setupStatus !== "ready" && <p role="status">{en ? "Complete the request in System Settings." : "请在系统设置中完成授权。"}</p>}
      </section>}
      <details className="kaus-computer-use-details"><summary>{en ? "Connected capabilities" : "已接入能力"}<ChevronRight size={13} aria-hidden /></summary>
        {snapshot.agents.length > 0 && <table className="kaus-computer-use-table"><thead><tr><th>Agent</th><th>{en ? "Browser" : "浏览器"}</th><th>{en ? "Desktop" : "桌面"}</th></tr></thead>
          <tbody>{snapshot.agents.map(agent => <tr key={agent.id}><th scope="row">{agent.label}</th><td>{capabilityStatus(agent.browser, true)}</td><td>{capabilityStatus(agent.desktop, true)}</td></tr>)}</tbody></table>}
        {snapshot.fallbacks.filter(provider => provider.capability === "browser").map(provider => <div className="kaus-settings-preference-row kaus-computer-use-provider" key={provider.provider}>
          <span>{provider.provider === "playwright" ? "Playwright" : provider.provider}</span><span>{capabilityStatus(provider)}</span>
        </div>)}
      </details>
    </>}
  </details>;
}
