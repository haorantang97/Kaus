import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Check, Folder, LoaderCircle, RefreshCw, Square, X } from "lucide-react";
import { useLocale, type DictKey } from "../i18n";
import { Modal } from "./ui";
import { useWorkbenchRuntime } from "../hooks/useWorkbenchRuntime";
import { WorkbenchRuntimeFields } from "./WorkbenchRuntimeFields";
import { activeWorkbenchIntake, cancelWorkbenchIntake, fetchWorkbenchIntake, installWorkbenchIntake, notifyWorkbenchChanged, retryWorkbenchIntake, startWorkbenchIntake, workbenchFailure, type WorkbenchIntake as Intake, type WorkbenchModule } from "../lib/workbenchApi";

export function WorkbenchIntake({ initial, onClose, onUpdate, onInstalled, onConversation }: {
  initial: Intake; onClose: () => void; onUpdate: (intake: Intake) => void;
  onInstalled: (module: WorkbenchModule) => void; onConversation: (conversationId: string, intakeId: string) => void;
}) {
  const { t, locale } = useLocale();
  const [intake, setIntake] = useState(initial);
  const runtime = useWorkbenchRuntime(initial.configuration ?? {});
  const [grants, setGrants] = useState<string[]>([]);
  const [instruction, setInstruction] = useState(initial.instruction ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pollFailed, setPollFailed] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const callbacks = useRef({ onUpdate, onInstalled });
  callbacks.current = { onUpdate, onInstalled };
  const mounted = useRef(true);
  const actionPending = useRef(false);
  const startKey = useRef<{request: string; key: string} | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const active = activeWorkbenchIntake(intake);
  const retry = ["failed", "cancelled", "interrupted"].includes(intake.status);
  const ready = intake.status === "ready";
  function update(next: Intake) { setIntake(next); callbacks.current.onUpdate(next); }
  useEffect(() => {
    if (!active && !pollFailed) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function poll() {
      try {
        const next = await fetchWorkbenchIntake(initial.id, controller.signal);
        if (stopped || actionPending.current) return;
        setIntake(next); setPollFailed(false); callbacks.current.onUpdate(next);
        if (activeWorkbenchIntake(next)) timer = setTimeout(() => void poll(), 1500);
      } catch {
        if (!stopped) { setPollFailed(true); timer = setTimeout(() => void poll(), 4000); }
      }
    }
    timer = setTimeout(() => void poll(), refresh ? 0 : 800);
    return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [initial.id, active, pollFailed, refresh]);
  async function runAction(action: () => Promise<void>) {
    if (actionPending.current) return;
    actionPending.current = true; setBusy(true); setError(null);
    try { await action(); }
    catch (failure) { if (mounted.current) setError(workbenchFailure(failure, locale === "en")); }
    finally { actionPending.current = false; if (mounted.current) { setBusy(false); setRefresh(value => value + 1); } }
  }
  async function start() {
    if (!runtime.ready) return;
    // Identical retries share an identity; changed configuration starts a new request.
    const body = { ...runtime.value, instruction: instruction.trim() };
    const request = JSON.stringify({retry,...body});
    if (startKey.current?.request !== request) startKey.current = {request,key:crypto.randomUUID()};
    const next = await (retry ? retryWorkbenchIntake : startWorkbenchIntake)(intake.id, { ...body, idempotencyKey: startKey.current.key });
    startKey.current = null;
    if (mounted.current) update(next);
  }
  async function install() {
    const result = await installWorkbenchIntake(intake.id, { ...runtime.value, grants });
    notifyWorkbenchChanged();
    if (mounted.current) { callbacks.current.onUpdate(result.intake); callbacks.current.onInstalled(result.module); }
  }
  return <Modal onClose={onClose} width={520}><section className="workbench-config workbench-intake" role="dialog" aria-modal="true" aria-label={t("workbench.intake.title")}>
    <header><h2>{t("workbench.intake.title")}</h2><button className="kaus-chat-icon-action" aria-label={t("common.close")} onClick={onClose}><X size={18}/></button></header>
    <div className="workbench-intake-project"><Folder size={20}/><div><strong>{intake.moduleName || intake.displayName}</strong><span>{t((`workbench.intake.kind.${intake.kind}`) as DictKey)}</span></div>{ready && <Check size={18}/>}</div>
    {active ? <div className="workbench-intake-progress" role="status"><LoaderCircle size={22} className="kaus-ui-spin"/><span>{t((`workbench.intake.status.${intake.status}`) as DictKey)}</span></div> : <>
      <WorkbenchRuntimeFields runtime={runtime} disabled={busy}/>
      {!ready && <label>{t("workbench.intake.instruction")}<textarea aria-label={t("workbench.intake.instruction")} value={instruction} disabled={busy} maxLength={4000} placeholder={t("workbench.optional")} onChange={event => setInstruction(event.target.value)} rows={2}/></label>}
      {ready && !!intake.manifestPermissions.length && <fieldset><legend>{t("workbench.access")}</legend>{intake.manifestPermissions.map(permission => <label className="workbench-grant" key={permission}><input type="checkbox" checked={grants.includes(permission)} disabled={busy} onChange={event => setGrants(previous => event.target.checked ? [...previous, permission] : previous.filter(value => value !== permission))}/><span>{t((`workbench.grant.${permission}`) as DictKey)}</span></label>)}</fieldset>}
      {retry && <p className="workbench-inline-error" role="status">{intake.status === "failed" ? workbenchFailure({code:intake.errorCode}, locale === "en") : t((`workbench.intake.status.${intake.status}`) as DictKey)}</p>}
    </>}
    {pollFailed && <div className="workbench-inline-error" role="alert"><span>{t("workbench.loadFailed")}</span><button className="kaus-chat-icon-action" aria-label={t("workbench.retry")} onClick={() => setRefresh(value => value + 1)}><RefreshCw size={15}/></button></div>}
    {error && <p className="workbench-inline-error" role="alert">{error}</p>}
    <footer>
      {intake.conversationId && <button className="workbench-subtle" onClick={() => onConversation(intake.conversationId!, intake.id)}><ArrowUpRight size={16}/>{t(intake.status === "waiting" ? "workbench.approval" : "workbench.details")}</button>}
      {active ? <button className="workbench-subtle" disabled={busy} onClick={() => void runAction(async () => { const next = await cancelWorkbenchIntake(intake.id); if (mounted.current) update(next); })}>{busy ? <LoaderCircle size={16} className="kaus-ui-spin"/> : <Square size={13}/>} {t("workbench.cancel")}</button> : ready ? <button className="workbench-primary" disabled={busy || runtime.busy || runtime.loading || (grants.includes("agent:run") && !runtime.ready)} onClick={() => void runAction(install)}>{busy ? <LoaderCircle size={16} className="kaus-ui-spin"/> : <Check size={16}/>} {t("workbench.intake.finish")}</button> : <button className="workbench-primary" disabled={busy || !runtime.ready} onClick={() => void runAction(start)}>{busy ? <LoaderCircle size={16} className="kaus-ui-spin"/> : retry ? <RefreshCw size={16}/> : <ArrowUpRight size={16}/>} {t(retry ? "workbench.intake.retry" : "workbench.intake.start")}</button>}
    </footer>
  </section></Modal>;
}
