import { engineAcceptsNewWork } from "../lib/engineAvailability";
import { EngineIcon } from "./EngineIcon";
import { RuntimeVersions } from "./RuntimeVersions";
import { useEffect, useState } from "react";
import { Check, Copy, ExternalLink, FolderOpen, LoaderCircle, RefreshCw, Search, Terminal } from "lucide-react";

import { useLocale, type DictKey } from "../i18n";
import { Button, ErrorNotice, Modal, ModalTitle, cream, inputStyle, toast } from "./ui";
import type { AttachableBackend } from "./EnginePanel";
import { chooseNativeExecutable, connectEngine, fetchEngineCatalog, groupEnginePresets, openEngineLogin, refreshEngineConnections, type EnginePreset } from "../lib/enginePresets";
import { describeFailure } from "../lib/sessionApi";

function statusKey(preset: EnginePreset): DictKey {
  if (preset.probeState === "available") return "engines.connect.available";
  if (preset.registered) return "engines.connect.registered";
  return preset.detected ? "engines.connect.detected" : "engines.connect.notDetected";
}

function CommandRow({ label, command }: { label: string; command: string }) {
  const { t } = useLocale();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1800);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const copy = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(command);
      setCopied(true);
    } catch {
      toast(t("engines.connect.copyFailed"), "bad");
    }
  };
  return (
    <div className="mt-4">
      <div className="mb-1.5 text-xs" style={{ color: cream(52) }}>{label}</div>
      <div className="flex items-start gap-2 rounded-lg p-2.5" style={{ background: cream(4) }}>
        <code className="min-w-0 flex-1 break-all text-xs" style={{ color: cream(80) }}>{command}</code>
        <button
          type="button"
          aria-label={t(copied ? "conversation.action.copied" : "conversation.action.copy") + " · " + label}
          title={t(copied ? "conversation.action.copied" : "conversation.action.copy")}
          onClick={() => void copy()}
          className="shrink-0 rounded p-0.5 hover:bg-white/5"
          style={{ color: cream(55) }}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
        </button>
      </div>
    </div>
  );
}

export function ConnectEngineModal({
  onClose,
  attachable = [],
  onAttach,
  initialBackendId,
}: {
  onClose: () => void;
  attachable?: AttachableBackend[];
  onAttach?: (backendId: string) => void;
  initialBackendId?: string;
}) {
  const { t } = useLocale();
  const [catalog, setCatalog] = useState<EnginePreset[]>([]);
  const [current, setCurrent] = useState("");
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [paths, setPaths] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<Record<string, boolean>>({});
  const [pendingLogin, setPendingLogin] = useState<string | null>(null);

  useEffect(() => {
    if (!pendingLogin) return;
    let cancelled = false;
    let refreshing = false;
    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        await refreshEngineConnections([pendingLogin]);
        if (!cancelled) { setPendingLogin(null); setRevision(value => value + 1); }
      } catch (failure) { if (!cancelled) setError(describeFailure(failure)); }
      finally { refreshing = false; }
    };
    window.addEventListener("focus", refresh);
    return () => { cancelled = true; window.removeEventListener("focus", refresh); };
  }, [pendingLogin]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    (initialBackendId ? fetchEngineCatalog(controller.signal, initialBackendId) : fetchEngineCatalog(controller.signal))
      .then(({ engines }) => { if (!controller.signal.aborted) setCatalog(engines); })
      .catch((failure: unknown) => { if (!controller.signal.aborted) setError(describeFailure(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision, initialBackendId]);

  const entries: EnginePreset[] = [...catalog, ...attachable
    .filter(engineAcceptsNewWork)
    .filter((backend) => !catalog.some((row) => row.backendId === backend.id))
    .map((backend): EnginePreset => ({
      id: `custom:${backend.id}`, label: backend.displayName, backendId: backend.id,
      registered: true, detected: false, installed: null, probeState: "unknown",
    }))];
  const filtered = groupEnginePresets(entries.filter(entry => engineAcceptsNewWork(entry) || (!onAttach && entry.registered && entry.backendId === initialBackendId))).filter((row) => `${row.label} ${row.variants.map((entry) => `${entry.label} ${entry.id}`).join(" ")}`.toLowerCase().includes(query.trim().toLowerCase()));
  const selectedGroup = filtered.find((row) => row.variants.some((entry) => current ? entry.id === current : entry.backendId === initialBackendId)) ?? filtered[0];
  const preset = selectedGroup?.variants.find((entry) => current ? entry.id === current : entry.backendId === initialBackendId) ?? selectedGroup?.preferred;
  const attached = !!onAttach && !!preset?.registered && !attachable.some((row) => row.id === preset.backendId);
  const path = preset ? paths[preset.id] ?? preset.nativePath ?? "" : "";
  const pathChanged = !!preset?.canSelectExecutable && !!path.trim() && path.trim() !== preset.nativePath;
  const showDetails = !!(preset?.nativePath || preset?.version || preset?.runtimeIdentity?.engineVersion || preset?.runtimeIdentity?.adapterVersion || preset?.runtimeIdentity?.protocolVersion != null);
  const refresh = async () => {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      if (catalog.some(row => row.registered)) await refreshEngineConnections();
      setPendingLogin(null); setRevision(value => value + 1);
    } catch (failure) { setError(describeFailure(failure)); }
    finally { setBusy(false); }
  };
  const choosePath = async () => {
    if (!preset) return;
    try {
      const selected = await chooseNativeExecutable(path || null);
      if (selected) { setPaths(value => ({ ...value, [preset.id]: selected })); setEditing(value => ({ ...value, [preset.id]: false })); }
      else if (selected === undefined) setEditing(value => ({ ...value, [preset.id]: true }));
    } catch (failure) { setError(describeFailure(failure)); }
  };
  const login = async () => {
    if (!preset || busy) return;
    setBusy(true); setError(null);
    try {
      await openEngineLogin(preset.backendId);
      setPendingLogin(preset.backendId);
      toast(t("engines.connect.loginOpened"), "ok");
    } catch (failure) { setError(describeFailure(failure)); }
    finally { setBusy(false); }
  };
  const connect = async () => {
    if (!preset || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = pathChanged ? await connectEngine(preset.id, path.trim()) : preset.registered ? { backendId: preset.backendId } : await connectEngine(preset.id);
      setCatalog((rows) => rows.map((row) => row.id === preset.id ? { ...row, backendId: result.backendId, registered: true, ...(pathChanged ? { nativePath: path.trim(), detected: true } : {}) } : row));
      if (onAttach) onAttach(result.backendId);
      else { toast(t("engines.connect.registered"), "ok"); if (!preset.registered) onClose(); }
    } catch (failure) {
      setError(describeFailure(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal onClose={onClose} width={720}>
      <div data-testid="connect-engine-modal">
        <ModalTitle>{t("engines.connect.title")}</ModalTitle>
        <div className="mb-4 flex items-center gap-2">
          <Search size={15} style={{ color: cream(40) }} />
          <input
            type="search"
            aria-label={t("engines.connect.search")}
            placeholder={t("engines.connect.search")}
            value={query}
            disabled={busy}
            onChange={(event) => setQuery(event.target.value)}
            style={{ ...inputStyle(), flex: 1 }}
          />
          <button
            type="button"
            aria-label={t("engines.connect.refresh")}
            title={t("engines.connect.refresh")}
            disabled={loading || busy}
            onClick={() => void refresh()}
            className="rounded p-2 hover:bg-white/5 disabled:opacity-40"
            style={{ color: cream(55) }}
          >
            {loading ? <LoaderCircle size={15} className="animate-spin" /> : <RefreshCw size={15} />}
          </button>
        </div>
        {error && <ErrorNotice error={error} className="mb-3 text-xs" />}
        {loading && catalog.length === 0 ? (
          <p role="status" className="py-8 text-center text-xs" style={{ color: cream(50) }}>{t("ui.loading")}</p>
        ) : (
          <div className="flex min-h-64 gap-5">
            <div
              className="max-h-[52vh] w-44 shrink-0 overflow-y-auto pr-1"
              role="listbox"
              aria-label={t("engines.connect.pick")}
            >
              {filtered.map((row, index) => (
                <button
                  key={row.id}
                  type="button"
                  disabled={busy}
                  role="option"
                  aria-selected={row.id === selectedGroup?.id}
                  tabIndex={row.id === selectedGroup?.id ? 0 : -1}
                  data-testid={`connect-preset-${row.id}`}
                  onClick={() => setCurrent(row.preferred.id)}
                  onKeyDown={(event) => {
                    const next = event.key === "ArrowDown" ? (index + 1) % filtered.length
                      : event.key === "ArrowUp" ? (index + filtered.length - 1) % filtered.length
                      : event.key === "Home" ? 0 : event.key === "End" ? filtered.length - 1 : null;
                    if (next === null) return;
                    event.preventDefault();
                    setCurrent(filtered[next].preferred.id);
                    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
                  }}
                  className="mb-1 flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-xs"
                  style={{ background: row.id === selectedGroup?.id ? cream(8) : "transparent", color: row.id === selectedGroup?.id ? cream(95) : cream(60) }}
                >
                  <span className="inline-flex items-center gap-2"><EngineIcon engine={row.preferred.productId ?? (row.preferred.id.startsWith("custom:") ? row.preferred.backendId : row.preferred.id)} />{row.label}</span>
                  {row.variants.some((entry) => entry.registered) && <Check size={12} className="shrink-0" aria-label={t("engines.connect.registered")} />}
                </button>
              ))}
              {!filtered.length && <p className="p-2 text-xs" style={{ color: cream(45) }}>{t("engines.connect.empty")}</p>}
            </div>
            {preset && (
              <div className="min-w-0 flex-1" data-testid="connect-details">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="inline-flex items-center gap-2 text-sm font-medium" style={{ color: cream(95) }}><EngineIcon engine={preset.productId ?? (preset.id.startsWith("custom:") ? preset.backendId : preset.id)} size={20} />{selectedGroup?.label}</h3>
                  {preset.setupUrl && (
                    <a href={preset.setupUrl} target="_blank" rel="noreferrer" aria-label={t("engines.connect.docs")} title={t("engines.connect.docs")} className="rounded p-1.5 hover:bg-white/5" style={{ color: cream(55) }}>
                      <ExternalLink size={15} />
                    </a>
                  )}
                </div>
                <p className="mt-1 text-xs" style={{ color: cream(48) }}>{t(statusKey(preset))}</p>
                {preset.runtimeIdentity?.updateState === "pending_idle" && <p role="status" className="mt-2 text-xs">{t("engines.connect.pendingUpdate")}</p>}
                {preset.canSelectExecutable && <div className="mt-4">
                  <button type="button" disabled={busy} onClick={() => void choosePath()} className="inline-flex items-center gap-2 text-xs" style={{ color: cream(68) }}><FolderOpen size={14} />{t("engines.connect.manualPath")}</button>
                  {pathChanged && !editing[preset.id] && <span className="ml-3 text-xs" style={{ color: cream(58) }}>{path.split(/[\\/]/).filter(Boolean).at(-1)}</span>}
                  {editing[preset.id] && <details open className="mt-2 text-xs" style={{ color: cream(58) }}><summary className="cursor-pointer">{t("engines.connect.executable")}</summary><div className="mt-2 flex items-center gap-2">
                    <input aria-label={t("engines.connect.executable")} value={path} disabled={busy} onChange={event => setPaths(value => ({ ...value, [preset.id]: event.target.value }))} placeholder="/path/to/cli" style={{ ...inputStyle(), width: "100%", minWidth: 0 }} />
                    <button type="button" disabled={busy} aria-label={t("engines.connect.chooseExecutable")} title={t("engines.connect.chooseExecutable")} onClick={() => void choosePath()}><FolderOpen size={15} /></button>
                  </div></details>}
                </div>}
                {showDetails && <details className="mt-4 text-xs" style={{ color: cream(58) }}>
                  <summary className="cursor-pointer">{t("engines.connect.details")}</summary>
                  <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-2">
                    {preset.nativePath && <><dt>{t("engines.connect.executable")}</dt><dd className="break-all">{preset.nativePath}</dd></>}
                    {preset.runtimeIdentity?.engineVersion && <><dt>{t("engines.connect.engineVersion")}</dt><dd>{preset.runtimeIdentity.engineVersion}</dd></>}
                    {(preset.runtimeIdentity?.adapterVersion || preset.version) && <><dt>{t("engines.connect.adapterVersion")}</dt><dd>{preset.runtimeIdentity?.adapterVersion || preset.version}</dd></>}
                    {preset.runtimeIdentity?.protocolVersion != null && <><dt>{t("engines.connect.protocolVersion")}</dt><dd>{preset.runtimeIdentity.protocolVersion}</dd></>}
                  </dl>
                </details>}
                {preset.installCommand && !preset.detected && <details className="mt-4 text-xs" style={{ color: cream(58) }}><summary className="cursor-pointer">{t("engines.connect.install")}</summary><CommandRow key={`${preset.id}:install`} label={t("engines.connect.install")} command={preset.installCommand} /></details>}
                {preset.adapterInstallCommand && <details className="mt-4 text-xs" style={{ color: cream(58) }}><summary className="cursor-pointer">{t("engines.connect.adapterSetup")}</summary><RuntimeVersions key={preset.id} presetId={preset.id} /><CommandRow label={t("engines.connect.install")} command={preset.adapterInstallCommand} /></details>}
                {preset.loginCommand && <details className="mt-4 text-xs" style={{ color: cream(58) }}><summary className="cursor-pointer">{t("engines.connect.auth")}</summary><CommandRow key={`${preset.id}:login`} label={t("engines.connect.auth")} command={preset.loginCommand} /></details>}
                <div className="mt-5 flex items-center gap-2">
                  <Button variant="primary" disabled={busy || (!!onAttach && !engineAcceptsNewWork(preset)) || (attached && !pathChanged) || (!onAttach && preset.registered && !pathChanged)} onClick={() => void connect()}>
                    {busy ? t("engines.connect.connecting") : pathChanged && preset.registered ? t("engines.connect.savePath") : attached ? t("engines.connect.attached") : onAttach ? t("engines.connect.addToProject") : preset.registered ? t("engines.connect.registered") : t("engines.connect.connect")}
                  </Button>
                  {preset.registered && preset.loginAvailable && <Button disabled={busy || pathChanged} aria-label={t("engines.connect.openLogin")} title={t("engines.connect.openLogin")} onClick={() => void login()}><Terminal size={16} /></Button>}
                </div>
              </div>
            )}
          </div>
        )}
        <div className="mt-5 flex justify-end"><Button onClick={onClose}>{t("common.close")}</Button></div>
      </div>
    </Modal>
  );
}
