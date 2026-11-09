/* 项目页「指令」：一节一条，沿项目树继承（设计稿 2026-11-02 第 2 节，替代旧守则编辑器）。
 *
 * 读的是 `effective-capabilities` 里的 `instructions` 条目，写的是通用能力端点：
 * 本级保存 = PUT value，屏蔽 = PUT blocked，删除 / 恢复继承 / 解除屏蔽 = DELETE 本级那一行。
 * 新建在卡片右上（列表级），每行的操作收在「⋯」里。
 *
 * 卡片底部是送达：每个指令文件由哪些 Agent 读、启动时注入还是写进文件；文件里已有
 * 用户自己写的内容时，问一次导入还是保留（设计稿第 3、4 节）。 */

import { useEffect, useState } from "react";
import { FileText, Plus } from "lucide-react";

import { CardRowMenu } from "./CardRowMenu";

import { useLocale } from "../i18n";
import {
  instructionSections,
  INSTRUCTIONS_TYPE,
  newSectionId,
  sectionValue,
  type InstructionSection,
} from "../lib/instructionSections";
import {
  deleteProjectCapability,
  describeFailure,
  fetchEffectiveCapabilities,
  fetchInstructionFiles,
  importInstructionFile,
  putProjectCapability,
  SessionApiError,
  type EffectiveCapabilitiesWire,
  type InstructionFileWire,
} from "../lib/sessionApi";
import { backendDisplay } from "../lib/backend-display";
import { Button, Card, Pill, cream, inputStyle, toast } from "./ui";

type Action = "edit" | "override" | "block" | "restore" | "unblock" | "delete";

const ACTIONS: Record<InstructionSection["state"], Action[]> = {
  local: ["edit", "delete"],
  overridden: ["edit", "restore"],
  inherited: ["override", "block"],
  blocked: ["unblock"],
};

interface Draft { id: string; title: string; body: string; order?: number }

export function ProjectInstructionsEditor({
  projectId,
  effective,
  projectLabel,
  onChanged,
}: {
  projectId: string;
  effective: EffectiveCapabilitiesWire | null;
  projectLabel: (projectId: string) => string;
  onChanged: () => void | Promise<void>;
}) {
  const { t } = useLocale();
  const [parent, setParent] = useState<EffectiveCapabilitiesWire | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);

  // 本级屏蔽的节要从上级找回标题和正文。
  const ancestry = effective?.ancestry ?? [];
  const parentId = ancestry.length > 1 ? ancestry[ancestry.length - 2] : null;
  const needsParent = Boolean((effective?.blocked ?? []).some(b => b.capabilityType === INSTRUCTIONS_TYPE && b.blockedByProjectId === projectId));
  useEffect(() => {
    if (!parentId || !needsParent) { setParent(null); return; }
    const controller = new AbortController();
    fetchEffectiveCapabilities(parentId, {}, controller.signal).then(setParent).catch(() => setParent(null));
    return () => controller.abort();
  }, [parentId, needsParent, effective]);

  const sections = effective ? instructionSections(effective, parent) : [];
  const name = (section: InstructionSection) =>
    section.title ?? (section.fallback === "id" ? section.id : t(`instructions.fallback.${section.fallback}`));

  const run = async (work: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await work();
      await onChanged();
      toast(done, "ok");
      return true;
    } catch (failure) {
      toast(t("instructions.toast.failed", { error: describeFailure(failure) }), "bad");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!draft || !draft.body.trim()) return;
    const saved = await run(
      () => putProjectCapability(projectId, INSTRUCTIONS_TYPE, draft.id, { value: sectionValue(draft) }),
      t("instructions.toast.saved"),
    );
    if (saved) setDraft(null);
  };

  const pick = (section: InstructionSection, action: Action) => {
    if (action === "edit" || action === "override") {
      setDraft({ id: section.id, title: section.title ?? "", body: section.body, order: section.order });
      return;
    }
    if (action === "block") {
      void run(() => putProjectCapability(projectId, INSTRUCTIONS_TYPE, section.id, { blocked: true }), t("instructions.toast.blocked"));
      return;
    }
    void run(() => deleteProjectCapability(projectId, INSTRUCTIONS_TYPE, section.id), t(`instructions.toast.${action}`));
  };

  const add = t("instructions.add");
  const editor = draft && (
    <div className="kaus-instruction-editor">
      <input value={draft.title} onChange={e => setDraft({ ...draft, title: e.target.value })}
        placeholder={t("instructions.field.title")} aria-label={t("instructions.field.title")} style={inputStyle()} />
      <textarea value={draft.body} onChange={e => setDraft({ ...draft, body: e.target.value })} rows={6} autoFocus
        placeholder={t("instructions.field.body")} aria-label={t("instructions.field.body")}
        style={{ ...inputStyle(), resize: "vertical", lineHeight: 1.6 }}
        onKeyDown={e => {
          if (e.key === "Escape") setDraft(null);
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void save();
        }} />
      <div className="flex justify-end gap-2">
        <Button onClick={() => setDraft(null)} disabled={busy}>{t("common.cancel")}</Button>
        <Button variant="primary" onClick={() => void save()} disabled={busy || !draft.body.trim()}>{t("common.save")}</Button>
      </div>
    </div>
  );

  return (
    <Card title={t("instructions.title")} icon={FileText} right={
      <Button aria-label={add} title={add} disabled={busy || draft !== null}
        onClick={() => setDraft({ id: newSectionId(), title: "", body: "" })}><Plus size={15} /></Button>
    }>
      {effective === null ? (
        <div className="py-2 text-xs" style={{ color: cream(45) }}>{t("instructions.loading")}</div>
      ) : (
        <ul className="kaus-instructions" data-testid="project-instructions">
          {sections.map(section => (
            <li key={section.id} className={`kaus-instruction${section.state === "blocked" ? " is-blocked" : ""}`}>
              {draft?.id === section.id ? editor : <>
                <div className="kaus-instruction-head">
                  <span className="kaus-instruction-title">{name(section)}</span>
                  {section.state === "inherited" && <Pill tone="muted">{t("instructions.state.inherited", { name: projectLabel(section.sourceProjectId) })}</Pill>}
                  {section.state === "overridden" && <Pill tone="gold">{t("instructions.state.overridden")}</Pill>}
                  {section.state === "blocked" && <Pill tone="muted">{t("instructions.state.blocked")}</Pill>}
                  <CardRowMenu label={t("instructions.menu")} actions={ACTIONS[section.state]} busy={busy || draft !== null}
                    actionLabel={action => t(`instructions.action.${action}`)} onPick={action => pick(section, action)} />
                </div>
                {section.body && <p className="kaus-instruction-body">{section.body}</p>}
              </>}
            </li>
          ))}
          {draft && !sections.some(s => s.id === draft.id) && <li className="kaus-instruction">{editor}</li>}
          {sections.length === 0 && !draft && <li className="py-1 text-xs" style={{ color: cream(45) }}>{t("instructions.empty")}</li>}
        </ul>
      )}
      {effective !== null && <InstructionDelivery projectId={projectId} revision={effective} hasSections={sections.length > 0} onImported={onChanged} />}
    </Card>
  );
}

function InstructionDelivery({ projectId, revision, hasSections, onImported }: {
  projectId: string;
  /** 指令变了就重取（受管块是否已写入会跟着变）。 */
  revision: unknown;
  hasSections: boolean;
  onImported: () => void | Promise<void>;
}) {
  const { t } = useLocale();
  const [state, setState] = useState<{ files: InstructionFileWire[]; denied: string | null } | "no-workspace" | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    fetchInstructionFiles(projectId, controller.signal)
      .then(result => setState({ files: Array.isArray(result?.files) ? result.files : [], denied: result?.workspaceDenied ?? null }))
      .catch(failure => {
        if (controller.signal.aborted) return;
        setState(failure instanceof SessionApiError && failure.code === "workspace_required" ? "no-workspace" : null);
      });
    return () => controller.abort();
  }, [projectId, revision, reload]);

  const decide = async (file: string, mode: "import" | "keep") => {
    setBusy(file);
    try {
      await importInstructionFile(projectId, file, mode);
      if (mode === "import") await onImported();
      setReload(value => value + 1);
      toast(t(`instructions.delivery.${mode}Done`, { file }), "ok");
    } catch (failure) {
      toast(t("instructions.toast.failed", { error: describeFailure(failure) }), "bad");
    } finally {
      setBusy(null);
    }
  };

  if (state === null) return null;
  if (state === "no-workspace") {
    return hasSections ? <p className="kaus-instruction-delivery-note">{t("instructions.delivery.noWorkspace")}</p> : null;
  }
  if (state.files.length === 0) return null;
  const agents = (file: InstructionFileWire) => file.backends.map(id => backendDisplay(id).displayName).join("、");
  return (
    <div className="kaus-instruction-delivery" data-testid="instruction-delivery">
      <div className="kaus-instruction-delivery-title">{t("instructions.delivery.title")}</div>
      {state.files.map(file => (
        <div key={file.file} className="kaus-instruction-delivery-row">
          <div className="kaus-instruction-delivery-head">
            <span className="kaus-instruction-delivery-file">{file.file}</span>
            <span className="kaus-instruction-delivery-agents">{agents(file)}</span>
            <Pill tone="muted">{t(file.deliveryChannel === "launch" ? "instructions.delivery.launch"
              : file.hasManagedBlock ? "instructions.delivery.written" : "instructions.delivery.onStart")}</Pill>
          </div>
          {file.pending && !state.denied && (
            <div className="kaus-instruction-delivery-pending">
              <span>{t("instructions.delivery.pending", { file: file.file })}</span>
              {file.preview && <p className="kaus-instruction-body">{file.preview}</p>}
              <div className="flex justify-end gap-2">
                <Button disabled={busy !== null} onClick={() => void decide(file.file, "keep")}>{t("instructions.delivery.keep")}</Button>
                <Button variant="primary" disabled={busy !== null} onClick={() => void decide(file.file, "import")}>{t("instructions.delivery.import")}</Button>
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
