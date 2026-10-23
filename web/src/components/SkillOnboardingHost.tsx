import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useLocale } from "../i18n";
import { fetchSkillOnboarding, finishSkillOnboarding, SKILL_ONBOARDING_EVENT, skillOnboardingText, type SkillInvitation } from "../lib/skillOnboarding";
import { EngineIcon } from "./EngineIcon";
import { ProjectSkillTree } from "./ProjectSkillTree";
import { Button, Modal, cream } from "./ui";

export function SkillAllocationDialog({ invitation, onFinish }: { invitation: SkillInvitation; onFinish: () => void }) {
  const { locale } = useLocale();
  const text = skillOnboardingText[locale];
  const [saving, setSaving] = useState(false);
  const [treeBusy, setTreeBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  const working = useRef(false);
  useLayoutEffect(() => {
    const previous = document.activeElement;
    dialog.current?.querySelector<HTMLInputElement>("input")?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  const finish = async (outcome: "skipped" | "done") => {
    if (working.current || treeBusy) return;
    working.current = true; setSaving(true); setFailed(false);
    try { await finishSkillOnboarding(invitation.id, outcome); onFinish(); }
    catch { setFailed(true); }
    finally { working.current = false; setSaving(false); }
  };
  return <Modal onClose={() => void finish("skipped")} width={820}>
    <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="skill-onboarding-title" data-testid="skill-onboarding-dialog"
      onKeyDown={event => {
        if (event.key !== "Tab") return;
        const elements = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),[tabindex="0"]')];
        const first = elements[0], last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
      <h2 id="skill-onboarding-title" className="flex items-center gap-2 text-base font-semibold"><EngineIcon engine={invitation.agentKey} size={20} />{invitation.agentLabel} · {text.title}</h2>
      <p className="mt-2 mb-2 text-xs" style={{ color: cream(58) }}>{invitation.skillCount} {text.skills} · {text.hint}</p>
      <div className="flex min-h-64" style={{ height: "min(52vh, 520px)" }}>
        <ProjectSkillTree focusProject={invitation.rootProjectId} initialAgent={invitation.agentKey} onBusyChange={setTreeBusy} />
      </div>
      {failed && <p role="alert" className="mb-2 text-xs" style={{ color: "var(--danger)" }}>{text.failure}</p>}
      <div className="flex justify-end gap-2"><Button disabled={saving || treeBusy} onClick={() => void finish("skipped")}>{text.skip}</Button><Button variant="primary" disabled={saving || treeBusy} onClick={() => void finish("done")}>{text.done}</Button></div>
    </div>
  </Modal>;
}

/** Only pending user-initiated connections are considered. No perpetual polling. */
export function SkillOnboardingHost() {
  const [invitation, setInvitation] = useState<SkillInvitation | null>(null);
  const [queued, setQueued] = useState<SkillInvitation | null>(null);
  const [revision, setRevision] = useState(0);
  const dismissed = useRef(new Set<string>());
  useEffect(() => {
    let controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let remaining = 12;
    let request = 0;
    const check = async () => {
      const sequence = ++request;
      try {
        const result = await fetchSkillOnboarding(controller.signal);
        if (controller.signal.aborted || sequence !== request) return;
        const next = result.items.find(item => !dismissed.current.has(item.id));
        if (next) setQueued(next);
        else if (result.waiting && --remaining > 0) timer = setTimeout(() => void check(), 5000);
      } catch { /* Passive discovery never creates a client error or retry toast. */ }
    };
    const restart = () => {
      controller.abort(); controller = new AbortController();
      if (timer) clearTimeout(timer);
      remaining = 12; void check();
    };
    const onVisible = () => { if (document.visibilityState === "visible") restart(); };
    void check();
    window.addEventListener(SKILL_ONBOARDING_EVENT, restart);
    window.addEventListener("focus", restart);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      controller.abort(); ++request; if (timer) clearTimeout(timer);
      window.removeEventListener(SKILL_ONBOARDING_EVENT, restart);
      window.removeEventListener("focus", restart);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [revision]);
  useEffect(() => {
    if (!queued || invitation) return;
    const show = () => {
      if (document.querySelector('[data-kaus-modal="true"], [role="dialog"], [aria-modal="true"], [data-testid="connect-engine-modal"]')) return;
      setInvitation(queued); setQueued(null);
    };
    show();
    const observer = new MutationObserver(show);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [queued, invitation]);
  const finish = useCallback(() => {
    if (invitation) dismissed.current.add(invitation.id);
    setInvitation(null); setQueued(null); setRevision(value => value + 1);
  }, [invitation]);
  return invitation ? <SkillAllocationDialog key={invitation.id} invitation={invitation} onFinish={finish} /> : null;
}
