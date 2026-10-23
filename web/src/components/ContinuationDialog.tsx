import { engineAcceptsNewWork } from "../lib/engineAvailability";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { FileText, GitBranch, LoaderCircle } from "lucide-react";
import { useLocale } from "../i18n";
import { Modal, ModalTitle, Button, inputStyle } from "./ui";
import { createContinuation, previewContinuation, type ContinuationPreview, type ContinuationSource } from "../lib/continuations";
import { describeFailure, ensureProjectBinding, fetchConnectedBackends } from "../lib/sessionApi";
import { preferredEngineConnections, type EngineConnection } from "../lib/enginePresets";

export function ContinuationDialog({ source, onClose, onCreated }: {
  source: ContinuationSource; onClose: () => void; onCreated: (id: string) => void;
}) {
  const { t } = useLocale();
  const [engines, setEngines] = useState<EngineConnection[]>([]);
  const [backend, setBackend] = useState("");
  const [preview, setPreview] = useState<ContinuationPreview | null>(null);
  const [context, setContext] = useState("");
  const [assets, setAssets] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef(0);
  const dialogRef = useRef<HTMLDivElement>(null);
  const agentRef = useRef<HTMLSelectElement>(null);
  // Capture the trigger before a parent dialog becomes inert.
  const returnFocusRef = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  useEffect(() => {
    agentRef.current?.focus();
    const trigger = returnFocusRef.current;
    return () => { if (trigger?.isConnected) trigger.focus({ preventScroll: true }); };
  }, []);
  const containFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), select:not([disabled]), textarea:not([disabled]), input:not([disabled]), a[href], summary, [tabindex]:not([tabindex="-1"])',
    ) || []).filter(control => !control.hidden && !control.closest('[hidden], [inert]'));
    const first = controls[0], last = controls.at(-1);
    if (!first || !last) {
      event.preventDefault(); dialogRef.current?.focus();
    } else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  };
  useEffect(() => {
    const controller = new AbortController();
    void fetchConnectedBackends(controller.signal).then(result => {
      if (!controller.signal.aborted) setEngines(preferredEngineConnections(result.backends.filter(engineAcceptsNewWork)));
    }).catch(failure => { if (!controller.signal.aborted) setError(describeFailure(failure)); });
    return () => controller.abort();
  }, []);
  const choose = async (id: string) => {
    const request = ++requestId.current;
    setBackend(id); setPreview(null); setBusy(true); setError("");
    try {
      const binding = await ensureProjectBinding(source.projectId, id);
      const next = await previewContinuation(source, binding.id);
      if (request !== requestId.current) return;
      setPreview(next); setContext(next.context);
      let bytes = 0;
      setAssets((next.assets || []).filter(item => {
        if (!item.available || !item.compatible || bytes + item.size > 25 * 1024 * 1024) return false;
        bytes += item.size; return true;
      }).slice(0, 8).map(item => item.id));
    } catch (failure) { if (request === requestId.current) setError(describeFailure(failure)); }
    finally { if (request === requestId.current) setBusy(false); }
  };
  const create = async () => {
    if (!preview) return;
    setBusy(true); setError("");
    try { const result = await createContinuation(preview.token, context, assets); onCreated(result.conversationId); }
    catch (failure) { setError(describeFailure(failure)); }
    finally { setBusy(false); }
  };
  return <Modal onClose={busy ? () => undefined : onClose} width={680}>
    <div ref={dialogRef} role="dialog" aria-modal="true" aria-label={t("continuation.title")} tabIndex={-1} onKeyDown={containFocus}>
    <ModalTitle>{t("continuation.title")}</ModalTitle>
    <p className="mb-4 flex items-center gap-2 text-xs opacity-60"><GitBranch size={14} aria-hidden />{t("continuation.newThread")}</p>
    <label className="mb-4 flex items-center gap-3 text-sm">Agent
      <select ref={agentRef} style={inputStyle()} aria-label={t("continuation.target")} disabled={busy} value={backend} onChange={event => void choose(event.target.value)}>
        <option value="" disabled>{t("continuation.target")}</option>
        {engines.map(engine => <option key={engine.id} value={engine.id}>{engine.productName || engine.displayName}</option>)}
      </select>
    </label>
    {busy && !preview && <LoaderCircle className="animate-spin" size={16} aria-label={t("ui.loading")} />}
    {preview && <>
      <div className="mb-2 flex items-center justify-between text-xs opacity-60"><label htmlFor="continuation-context">{t("continuation.context")}</label>
        <span>{t("continuation.count", { included: preview.includedMessages, total: preview.totalMessages })}</span></div>
      <textarea id="continuation-context" value={context} disabled={busy} maxLength={preview.maxChars} onChange={event => setContext(event.target.value)}
        className="min-h-64 w-full resize-y rounded-lg p-3 text-sm leading-relaxed" style={inputStyle()} />
      <p className="mt-2 flex items-center gap-2 text-xs opacity-60"><FileText size={14} aria-hidden />{t("continuation.files")}</p>
      {!!preview.assets?.some(item => item.available && item.compatible) && <div className="mt-3 max-h-28 space-y-1 overflow-auto text-xs">{preview.assets.filter(item => item.available && item.compatible).map(item => <label key={item.id} className="flex items-center gap-2">
        <input type="checkbox" checked={assets.includes(item.id)} disabled={busy || !item.compatible || (assets.length >= 8 && !assets.includes(item.id))}
          onChange={event => setAssets(current => event.target.checked ? [...current, item.id] : current.filter(id => id !== item.id))} />
        <span>{item.name}</span>
      </label>)}</div>}
      {!!(preview.capabilities.length || preview.excludedCapabilities?.length || preview.assets?.some(item => !item.compatible)) && <details className="mt-3 text-xs"><summary>{t("continuation.config")}</summary>
        {!!preview.capabilities.length && <ul className="mt-2 flex max-h-28 flex-wrap gap-2 overflow-auto">{preview.capabilities.map(item => <li className="rounded-md border border-current/10 px-2 py-1" key={`${item.type}:${item.name}`}>{item.name}</li>)}</ul>}
        {!!preview.excludedCapabilities?.length && <div className="mt-3 opacity-60"><span>{t("continuation.excluded")}</span><ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1">{preview.excludedCapabilities.map(item => <li key={`${item.type}:${item.name}`}>{item.name}</li>)}</ul></div>}
        {!!preview.assets?.some(item => !item.compatible) && <div className="mt-3 opacity-60"><span>{t("continuation.omittedFiles")}</span><ul className="mt-1">{preview.assets.filter(item => !item.compatible).map(item => <li key={item.id}>{item.name}</li>)}</ul></div>}
      </details>
      }
    </>}
    {error && <p role="alert" className="mt-3 text-xs">{error}</p>}
    <div className="mt-5 flex justify-end gap-2"><Button disabled={busy} onClick={onClose}>{t("common.cancel")}</Button>
      <Button variant="primary" disabled={busy || !preview || !context.trim()} onClick={() => void create()}>{t("continuation.create")}</Button></div>
    </div>
  </Modal>;
}
