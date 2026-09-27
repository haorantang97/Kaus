import { useEffect, useState } from "react";
import { Check, Copy, ExternalLink, LoaderCircle, RefreshCw, Search } from "lucide-react";

import { useLocale, type DictKey } from "../i18n";
import { Button, Modal, ModalTitle, cream, inputStyle, toast } from "./ui";
import type { AttachableBackend } from "./EnginePanel";
import { connectEngine, fetchEngineCatalog, type EnginePreset } from "../lib/enginePresets";
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
}: {
  onClose: () => void;
  attachable?: AttachableBackend[];
  onAttach?: (backendId: string) => void;
}) {
  const { t } = useLocale();
  const [catalog, setCatalog] = useState<EnginePreset[]>([]);
  const [current, setCurrent] = useState("");
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchEngineCatalog(controller.signal)
      .then(({ engines }) => { if (!controller.signal.aborted) setCatalog(engines); })
      .catch((failure: unknown) => { if (!controller.signal.aborted) setError(describeFailure(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);

  const entries: EnginePreset[] = [...catalog, ...attachable
    .filter((backend) => !catalog.some((row) => row.backendId === backend.id))
    .map((backend): EnginePreset => ({
      id: `custom:${backend.id}`, label: backend.displayName, backendId: backend.id,
      registered: true, detected: false, installed: null, probeState: "unknown",
    }))];
  const filtered = entries.filter((row) => `${row.label} ${row.id}`.toLowerCase().includes(query.trim().toLowerCase()));
  const preset = filtered.find((row) => row.id === current) ?? filtered[0];
  const attached = !!onAttach && !!preset?.registered && !attachable.some((row) => row.id === preset.backendId);
  const connect = async () => {
    if (!preset || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = preset.registered ? { backendId: preset.backendId } : await connectEngine(preset.id);
      setCatalog((rows) => rows.map((row) => row.id === preset.id ? { ...row, backendId: result.backendId, registered: true } : row));
      if (onAttach) onAttach(result.backendId);
      else toast(t("engines.connect.registered"), "ok");
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
            onChange={(event) => setQuery(event.target.value)}
            style={{ ...inputStyle(), flex: 1 }}
          />
          <button
            type="button"
            aria-label={t("engines.connect.refresh")}
            title={t("engines.connect.refresh")}
            disabled={loading || busy}
            onClick={() => setRevision((value) => value + 1)}
            className="rounded p-2 hover:bg-white/5 disabled:opacity-40"
            style={{ color: cream(55) }}
          >
            {loading ? <LoaderCircle size={15} className="animate-spin" /> : <RefreshCw size={15} />}
          </button>
        </div>
        {error && <p role="alert" className="mb-3 text-xs" style={{ color: "var(--danger, #d97676)" }}>{error}</p>}
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
                  role="option"
                  aria-selected={row.id === preset?.id}
                  tabIndex={row.id === preset?.id ? 0 : -1}
                  data-testid={`connect-preset-${row.id}`}
                  onClick={() => setCurrent(row.id)}
                  onKeyDown={(event) => {
                    const next = event.key === "ArrowDown" ? (index + 1) % filtered.length
                      : event.key === "ArrowUp" ? (index + filtered.length - 1) % filtered.length
                      : event.key === "Home" ? 0 : event.key === "End" ? filtered.length - 1 : null;
                    if (next === null) return;
                    event.preventDefault();
                    setCurrent(filtered[next].id);
                    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
                  }}
                  className="mb-1 flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left text-xs"
                  style={{ background: row.id === preset?.id ? cream(8) : "transparent", color: row.id === preset?.id ? cream(95) : cream(60) }}
                >
                  <span>{row.label}</span>
                  {row.registered && <Check size={12} className="shrink-0" aria-label={t("engines.connect.registered")} />}
                </button>
              ))}
              {!filtered.length && <p className="p-2 text-xs" style={{ color: cream(45) }}>{t("engines.connect.empty")}</p>}
            </div>
            {preset && (
              <div className="min-w-0 flex-1" data-testid="connect-details">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-medium" style={{ color: cream(95) }}>{preset.label}</h3>
                  {preset.setupUrl && (
                    <a href={preset.setupUrl} target="_blank" rel="noreferrer" aria-label={t("engines.connect.docs")} title={t("engines.connect.docs")} className="rounded p-1.5 hover:bg-white/5" style={{ color: cream(55) }}>
                      <ExternalLink size={15} />
                    </a>
                  )}
                </div>
                <p className="mt-1 text-xs" style={{ color: cream(48) }}>{t(statusKey(preset))}</p>
                {preset.installCommand && !preset.detected && <CommandRow key={`${preset.id}:install`} label={t("engines.connect.install")} command={preset.installCommand} />}
                {preset.loginCommand && <CommandRow key={`${preset.id}:login`} label={t("engines.connect.auth")} command={preset.loginCommand} />}
                <div className="mt-5">
                  <Button variant="primary" disabled={busy || attached || (!onAttach && preset.registered)} onClick={() => void connect()}>
                    {busy ? t("engines.connect.connecting") : attached ? t("engines.connect.attached") : onAttach ? t("engines.connect.addToProject") : preset.registered ? t("engines.connect.registered") : t("engines.connect.connect")}
                  </Button>
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
