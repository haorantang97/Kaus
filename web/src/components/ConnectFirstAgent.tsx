import { useEffect, useState } from "react";
import { Check, Copy, LoaderCircle } from "lucide-react";

import { useLocale } from "../i18n";
import { connectEngine, fetchEngineCatalog, groupEnginePresets, type EngineCatalogGroup } from "../lib/enginePresets";
import { engineAcceptsNewWork } from "../lib/engineAvailability";
import { describeFailure } from "../lib/sessionApi";
import { finishSkillOnboarding, type CapabilityInvitation } from "../lib/skillOnboarding";
import { CapabilityOnboardingStep } from "./CapabilityOnboardingStep";
import { ConnectEngineModal } from "./ConnectEngineModal";
import { EngineIcon } from "./EngineIcon";
import { ErrorNotice, Modal, toast } from "./ui";
import "./connect-first-agent.css";

/** Installed engines first, then the catalog's own order. */
export function firstAgentRows(groups: EngineCatalogGroup[]): EngineCatalogGroup[] {
  const installed = (group: EngineCatalogGroup) => group.variants.some(variant => variant.detected);
  return [...groups.filter(installed), ...groups.filter(group => !installed(group))];
}

/** Shown instead of the composer while no Agent is connected (2026-11-24「新用户引导与空环境」).
 *  One action per row: an installed engine connects; one that is not installed offers its install
 *  command. Login state is unknown before connecting; a sign-in it needs goes through the usual flow. */
export function ConnectFirstAgent({ onConnected }: { onConnected: (backendId: string) => void }) {
  const { t } = useLocale();
  const [groups, setGroups] = useState<EngineCatalogGroup[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [step, setStep] = useState<{ invitation: CapabilityInvitation; backendId: string } | null>(null);
  const [more, setMore] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    fetchEngineCatalog(controller.signal)
      .then(({ engines }) => { if (!controller.signal.aborted) setGroups(firstAgentRows(groupEnginePresets(engines.filter(engine => engineAcceptsNewWork(engine))))); })
      .catch(failure => { if (!controller.signal.aborted) setError(failure); });
    return () => controller.abort();
  }, []);

  const connect = async (group: EngineCatalogGroup) => {
    const preset = group.variants.find(variant => variant.detected) ?? group.preferred;
    if (busy) return;
    setBusy(group.id); setError(null);
    try {
      const result = await connectEngine(preset.id);
      toast(t("engines.connect.registered"), "ok");
      // First connection of a product: allocate its own capabilities before leaving.
      if (result.onboarding) setStep({ invitation: result.onboarding, backendId: result.backendId });
      else onConnected(result.backendId);
    } catch (failure) { setError(describeFailure(failure)); }
    finally { setBusy(null); }
  };

  return (
    <section className="kaus-first-agent" aria-labelledby="kaus-first-agent-title" data-testid="connect-first-agent">
      <h2 id="kaus-first-agent-title">{t("firstAgent.title")}</h2>
      {error ? <ErrorNotice error={error} className="kaus-first-agent-error" /> : null}
      {groups === null && !error && <p role="status" className="kaus-first-agent-loading"><LoaderCircle size={16} aria-hidden /></p>}
      {groups && <ul className="kaus-first-agent-list">
        {groups.map(group => {
          const installed = group.variants.some(variant => variant.detected);
          const command = group.variants.find(variant => variant.installCommand)?.installCommand;
          return <li key={group.id} className="kaus-first-agent-row">
            <EngineIcon engine={group.preferred.productId ?? group.preferred.backendId} size={20} />
            <span className="kaus-first-agent-name">{group.label}</span>
            <span className="kaus-first-agent-status">{t(installed ? "engines.connect.detected" : "engines.connect.notDetected")}</span>
            {installed
              ? <button type="button" className="kaus-first-agent-connect" disabled={!!busy} onClick={() => void connect(group)}>
                {busy === group.id ? <LoaderCircle size={14} aria-hidden /> : t("engines.connect.connect")}
              </button>
              : command ? <CopyCommand command={command} /> : <span className="kaus-first-agent-connect is-empty" aria-hidden />}
          </li>;
        })}
      </ul>}
      <button type="button" className="kaus-inline-action kaus-first-agent-more" onClick={() => setMore(true)}>{t("firstAgent.more")}</button>
      {more && <ConnectEngineModal onClose={() => setMore(false)} onAttach={backendId => { setMore(false); onConnected(backendId); }} />}
      {step && <Modal onClose={() => { void finishSkillOnboarding(step.invitation.id, "skipped").catch(() => undefined); onConnected(step.backendId); }} width={820}>
        <CapabilityOnboardingStep key={step.invitation.id} invitation={step.invitation} onFinish={() => onConnected(step.backendId)} />
      </Modal>}
    </section>
  );
}

function CopyCommand({ command }: { command: string }) {
  const { t } = useLocale();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1800);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const label = t("firstAgent.copyInstall", { command });
  return <button type="button" className="kaus-chat-icon-action kaus-first-agent-copy" aria-label={label} title={label}
    onClick={() => {
      void (navigator.clipboard?.writeText ? navigator.clipboard.writeText(command) : Promise.reject(new Error("clipboard")))
        .then(() => setCopied(true), () => toast(t("engines.connect.copyFailed"), "bad"));
    }}>
    {copied ? <Check size={15} aria-hidden /> : <Copy size={15} aria-hidden />}
  </button>;
}
