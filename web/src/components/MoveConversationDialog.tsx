import { useState } from "react";
import { useLocale } from "../i18n";
import { Button, Modal, ModalTitle, inputStyle } from "./ui";
import { ContinuationDialog } from "./ContinuationDialog";
import { describeFailure, ensureProjectBinding, moveConversation, SessionApiError, type ArchivedConversationWire, type ProjectWire } from "../lib/sessionApi";

/** Move an archived conversation of a deleted Project into an active Project (same engine).
 *  A different working folder or native setup is confirmed first; continuing with another
 *  Agent is offered instead because the native session may not resume there. */
export function MoveConversationDialog({ item, projects, initialProjectId, onClose, onMoved, onContinued }: {
  item: ArchivedConversationWire; projects: ProjectWire[]; initialProjectId?: string;
  onClose: () => void; onMoved: () => void; onContinued: (conversationId: string) => void;
}) {
  const { t } = useLocale();
  const choices = projects.filter(project => project.status !== "disabled" && project.id !== item.projectId);
  const [target, setTarget] = useState(initialProjectId && choices.some(p => p.id === initialProjectId) ? initialProjectId : "");
  const [warned, setWarned] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [continuing, setContinuing] = useState(false);

  const move = async () => {
    if (!target || !item.backendId) return;
    setBusy(true); setError("");
    try {
      // The same engine is ensured in the target; engine identity never changes on move.
      const binding = await ensureProjectBinding(target, item.backendId);
      await moveConversation(item.id, { projectId: target, bindingId: binding.id, confirmWorkspaceChange: warned });
      onMoved();
    } catch (failure) {
      if (failure instanceof SessionApiError && failure.code === "workspace_changed") setWarned(true);
      else setError(describeFailure(failure));
    } finally { setBusy(false); }
  };

  if (continuing) return <ContinuationDialog source={{ kind: "conversation", id: item.id, projectId: item.projectId }}
    targetProjectId={target} onClose={() => setContinuing(false)} onCreated={onContinued} />;
  return <Modal onClose={busy ? () => undefined : onClose} width={460}>
    <div role="dialog" aria-modal="true" aria-label={t("project.move")}>
      <ModalTitle>{t("project.move")}</ModalTitle>
      <label className="mb-3 flex items-center gap-3 text-sm">{t("project.move.target")}
        <select style={inputStyle()} aria-label={t("project.move.target")} disabled={busy} value={target}
          onChange={event => { setTarget(event.target.value); setWarned(false); setError(""); }}>
          <option value="" disabled>{t("draft.project.choose")}</option>
          {choices.map(project => <option key={project.id} value={project.id}>{project.displayName || project.slug}</option>)}
        </select>
      </label>
      {warned && <p role="alert" className="mb-2 text-xs leading-relaxed" style={{ color: "var(--danger)" }}>{t("project.move.workspaceWarning")}</p>}
      {error && <p role="alert" className="mb-2 text-xs">{error}</p>}
      <div className="mt-4 flex justify-end gap-2">
        <Button disabled={busy} onClick={onClose}>{t("common.cancel")}</Button>
        {warned && <Button disabled={busy} onClick={() => setContinuing(true)}>{t("project.move.continue")}</Button>}
        <Button variant="primary" disabled={busy || !target || !item.backendId} onClick={() => void move()}>
          {warned ? t("project.move.anyway") : t("project.move")}
        </Button>
      </div>
    </div>
  </Modal>;
}
