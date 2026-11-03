import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useLocale } from "../i18n";
import { fetchCapabilityOnboarding, finishSkillOnboarding, skillOnboardingText, type CapabilityInvitation, type CapabilityOnboardingStatus } from "../lib/skillOnboarding";
import { EngineIcon } from "./EngineIcon";
import { ProjectSkillTree } from "./ProjectSkillTree";
import { Button, Spinner, cream } from "./ui";

const POLL_MS = 2000;
const POLL_LIMIT = 30;

/**
 * Connect-flow step for a newly connected Agent: arrange its native Skills and
 * MCP servers with the existing Project tree. Nothing found finishes silently;
 * there is no popup outside the connect flow.
 */
export function CapabilityOnboardingStep({ invitation, footerStart, onFinish }: {
  invitation: CapabilityInvitation;
  footerStart?: ReactNode;
  onFinish: () => void;
}) {
  const { locale } = useLocale();
  const text = skillOnboardingText[locale];
  const [status, setStatus] = useState<CapabilityOnboardingStatus | null>(null);
  const [saving, setSaving] = useState(false);
  const [treeBusy, setTreeBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const working = useRef(false);
  const finished = useRef(onFinish);
  finished.current = onFinish;
  const body = useRef<HTMLDivElement>(null);
  const showTree = !!status && status.ready && status.status === "pending" && !!status.rootProjectId && status.skillCount + status.mcpCount > 0;

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const check = async () => {
      try {
        const next = await fetchCapabilityOnboarding(invitation.id, controller.signal);
        if (controller.signal.aborted) return;
        if (next.status !== "pending" || (next.ready && next.skillCount + next.mcpCount === 0)) { finished.current(); return; }
        setStatus(next);
        if (next.ready) return;
      } catch {
        if (controller.signal.aborted) return;
      }
      if (++attempts < POLL_LIMIT) timer = setTimeout(() => void check(), POLL_MS);
    };
    void check();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [invitation.id]);
  useLayoutEffect(() => {
    if (showTree) body.current?.querySelector<HTMLInputElement>("input")?.focus();
  }, [showTree]);

  const finish = async (outcome: "skipped" | "done") => {
    if (working.current || treeBusy) return;
    working.current = true; setSaving(true); setFailed(false);
    try { await finishSkillOnboarding(invitation.id, outcome); onFinish(); }
    catch { setFailed(true); }
    finally { working.current = false; setSaving(false); }
  };
  const counts = status ? [status.skillCount ? `${status.skillCount} ${text.skills}` : "", status.mcpCount ? `${status.mcpCount} ${text.mcp}` : ""].filter(Boolean).join(" · ") : "";

  return <div ref={body} role="group" aria-labelledby="capability-onboarding-title" data-testid="capability-onboarding-step">
    <h3 id="capability-onboarding-title" className="kaus-modal-title flex items-center gap-2"><EngineIcon engine={invitation.agentKey} size={20} />{invitation.agentLabel} · {text.title}</h3>
    {showTree ? <>
      <p className="mt-1 mb-2 text-xs" style={{ color: cream(58) }}>{counts} · {text.hint}</p>
      <div className="flex min-h-64" style={{ height: "min(52vh, 520px)" }}>
        <ProjectSkillTree focusProject={status.rootProjectId} initialAgent={invitation.agentKey} onBusyChange={setTreeBusy} />
      </div>
    </> : <p role="status" className="flex min-h-40 items-center justify-center gap-2 text-xs" style={{ color: cream(50) }}><Spinner />{text.loading}</p>}
    {failed && <p role="alert" className="mt-2 text-xs" style={{ color: "var(--danger)" }}>{text.failure}</p>}
    <div className="mt-4 flex items-center gap-2">
      {footerStart}
      <span className="flex-1" />
      <Button disabled={saving || treeBusy} onClick={() => void finish("skipped")}>{text.skip}</Button>
      <Button variant="primary" disabled={!showTree || saving || treeBusy} onClick={() => void finish("done")}>{text.done}</Button>
    </div>
  </div>;
}
