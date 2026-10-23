import { engineAcceptsNewWork } from "../lib/engineAvailability";
import { ErrorNotice } from "./ui";
import { ProjectAgentPicker } from "./ProjectAgentPicker";
import "./coordinator.css";
import { projectPath } from "../lib/routes";
import { useEffect, useState, type ReactNode } from "react";
import { ArrowUpRight, ChevronDown, Settings2 } from "lucide-react";
import { BarMenu, approvalLabel, EngineConfigFields, WorkspacePill } from "./ComposerBar";
import { Button, Modal, ModalTitle, inputStyle } from "./ui";
import { useLocale } from "../i18n";
import { fetchModelCatalog, fetchEffectiveSettings, sessionRequest,
  type BindingWire, type ModelCatalogWire, type EffectiveSettingsWire } from "../lib/sessionApi";
import { refreshGroups } from "../lib/groupStore";
import type { GroupWire } from "../lib/groupsApi";
import { modelOptionsFor, modelSelection } from "../lib/modelCatalogView";

export interface CoordinatorConfig {
  bindingId: string; modelId?: string | null; providerId?: string | null;
  reasoningMode?: string | null; approvalMode?: string | null; workspaceRoot?: string | null;
  executionMode?: string | null;
  configOptions?: Record<string, string | boolean>;
  instructions?: string; callCap?: number; timeLimitMinutes?: number; revision?: number; sourceRevision?: number | null;
}
type ConfigurationView = { retainedBinding?: BindingWire & { projectName: string }; config: CoordinatorConfig | null; candidate?: CoordinatorConfig | null; defaults?: CoordinatorConfig | null };
const defaultsPath = "/api/group-settings/coordinator";
const pathFor = (groupId?: string) => groupId ? `/api/groups/${encodeURIComponent(groupId)}/coordinator` : defaultsPath;
const modelKey = (model: string, provider?: string | null) => JSON.stringify([provider ?? null, model]);
const engineDefaultKey = JSON.stringify([null, null]);

function CoordinatorField({ label, children, wide = false }: { label: string; children: ReactNode; wide?: boolean }) {
  return <div className={`kaus-coordinator-field${wide ? " is-wide" : ""}`}><span>{label}</span>{children}</div>;
}

export function CoordinatorSettings({ group, global = false }: { group?: GroupWire; global?: boolean }) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<CoordinatorConfig>({ bindingId: "" });
  const [revision, setRevision] = useState(0);
  const [defaults, setDefaults] = useState<CoordinatorConfig | null>(null);
  const [bindings, setBindings] = useState<(BindingWire & { projectName: string })[]>([]);
  const [projects, setProjects] = useState<{ id: string; displayName: string }[]>([]);
  const [projectId, setProjectId] = useState("");
  const [engineIds, setEngineIds] = useState<string[] | undefined>();
  const [catalog, setCatalog] = useState<ModelCatalogWire | null>(null);
  const [effective, setEffective] = useState<EffectiveSettingsWire | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [more, setMore] = useState(false);
  const groupConfig = group?.settings?.coordinator as CoordinatorConfig | undefined;
  const running = group?.thread?.status === "running" || group?.thread?.status === "closing";
  const selectedBinding = bindings.find(b => b.id === draft.bindingId);
  useEffect(() => {
    if (!group) return;
    const show = (event: Event) => { if ((event as CustomEvent).detail === group.id) setOpen(true); };
    window.addEventListener("kaus:configure-coordinator", show);
    return () => window.removeEventListener("kaus:configure-coordinator", show);
  }, [group?.id]);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(""); setLoading(true);
    Promise.all([sessionRequest<ConfigurationView>(pathFor(group?.id)),
      sessionRequest<{ bindings: (BindingWire & { projectName: string })[]; projects?: { id: string; displayName: string }[]; engineIds?: string[] }>(`${defaultsPath}/options`)]).then(([view, options]) => {
      if (cancelled) return;
      const availableBindings = view.retainedBinding ? [...options.bindings, view.retainedBinding] : options.bindings;
      setBindings(availableBindings);
      const availableProjects = options.projects ?? [...new Map(options.bindings.map(b => [b.projectId, { id: b.projectId, displayName: b.projectName }])).values()];
      setProjects(availableProjects); setEngineIds(options.engineIds);
      setRevision(view.config?.revision ?? 0);
      setDefaults(view.defaults ?? (global ? view.config : null));
      const candidate = view.config ?? view.candidate ?? view.defaults;
      const selected = availableBindings.find(b => b.id === candidate?.bindingId);
      setProjectId(selected?.projectId ?? availableProjects[0]?.id ?? "");
      setDraft(candidate && selected ? candidate : { bindingId: "" });
    }).catch(e => { if (!cancelled) setError(e.message); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, group?.id, global]);
  useEffect(() => {
    const binding = selectedBinding;
    setCatalog(null); setEffective(null);
    if (!open || !binding) return;
    let cancelled = false;
    Promise.all([fetchModelCatalog(binding.backendId, binding.id), fetchEffectiveSettings(binding.id)])
      .then(([models, settings]) => { if (!cancelled) { setCatalog(models); setEffective(settings); } })
      .catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [open, selectedBinding?.id, selectedBinding?.backendId]);
  const models = catalog?.models.filter(model => model.availability !== "unavailable") ?? [];
  const engineDefault = models.length === 0 && catalog?.engineDefaultAvailable === true && catalog.degraded !== true;
  const catalogReady = !!catalog && (models.length > 0 || engineDefault);
  const catalogError = catalog && !catalogReady ? catalog.diagnostics?.[0] ?? "引擎未提供可用模型" : "";
  const modelOptions = modelOptionsFor(catalog, selectedBinding, draft.modelId ?? undefined);
  const selected = models.find(m => m.modelId === draft.modelId && (!draft.providerId || m.providerId === draft.providerId));
  const reasoning = selected?.reasoningLevels ?? (engineDefault ? effective?.conversationControls?.reasoningLevels : undefined) ?? effective?.reasoningEffort.levels ?? [];
  const approval = effective?.conversationControls?.approvalModes ?? [];
  const execution = effective?.conversationControls?.executionModes ?? [];
  const set = (change: Partial<CoordinatorConfig>) => setDraft(current => ({ ...current, ...change }));
  const save = async (asDefault = false, handoff = false) => {
    setBusy(true); setError("");
    try {
      if (asDefault) {
        const latest = await sessionRequest<ConfigurationView>(defaultsPath);
        const result = await sessionRequest<ConfigurationView>(defaultsPath, { method: "PUT", body: { config: draft, expectedRevision: latest.config?.revision ?? 0 } });
        setDefaults(result.config);
      } else {
        await sessionRequest(pathFor(group?.id), { method: "PUT", body: { config: draft, expectedRevision: revision } });
        if (handoff && group) await sessionRequest(`${pathFor(group.id)}/handoff`, { method: "POST", body: {} });
        await refreshGroups();
        setOpen(false);
        if (group) window.dispatchEvent(new CustomEvent("kaus:coordinator-configured", { detail: group.id }));
      }
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <>
    <button type="button" className="kaus-bar-pill kaus-coordinator-trigger" onClick={() => setOpen(true)} aria-label={global ? "默认组长" : "组长配置"} title={groupConfig?.modelId ? `组长 · ${groupConfig.modelId}` : global ? "默认组长" : "组长配置"}>
      <Settings2 size={14} aria-hidden /><span>{global ? "默认组长" : "组长"}</span>
    </button>
    {open && <Modal onClose={() => !busy && setOpen(false)} width={520}>
      <ModalTitle>{global ? "默认组长" : "组长配置"}</ModalTitle>
      <div className="kaus-coordinator-settings kaus-settings-sheet" aria-busy={loading}>
        {loading && <span role="status" className="kaus-group-dim">读取配置…</span>}
        <div className="kaus-coordinator-grid">
        <CoordinatorField label={t("draft.field.project")} wide><BarMenu floating label={t("draft.field.project")} value={projectId}
          display={projects.find(p => p.id === projectId)?.displayName ?? t("draft.project.choose")}
          options={projects.map(p => ({ value: p.id, label: p.displayName }))}
          onSelect={id => { setProjectId(id); setDraft(current => ({ bindingId: "", instructions: current.instructions, callCap: current.callCap, timeLimitMinutes: current.timeLimitMinutes })); }} /></CoordinatorField>
        <CoordinatorField label="Agent" wide><ProjectAgentPicker projectId={projectId} value={draft.bindingId} retainedBinding={!engineAcceptsNewWork(selectedBinding) ? selectedBinding : undefined} allowedBackendIds={engineIds} disabled={busy}
          onChange={binding => {
            if (!binding) { setDraft(current => ({ ...current, bindingId: "" })); return; }
            setBindings(current => [...current.filter(b => b.id !== binding.id), { ...binding, projectName: projects.find(p => p.id === binding.projectId)?.displayName ?? binding.projectId }]);
            setDraft(current => current.bindingId === binding.id ? current : { bindingId: binding.id, instructions: current.instructions, callCap: current.callCap, timeLimitMinutes: current.timeLimitMinutes });
          }} /></CoordinatorField>
        <CoordinatorField label="模型" wide><BarMenu floating label="模型" value={draft.modelId ? modelKey(draft.modelId, draft.providerId) : engineDefault ? engineDefaultKey : null}
          display={selected?.displayName ?? draft.modelId ?? (engineDefault ? "引擎默认" : "选择模型")}
          options={engineDefault ? [{ value: engineDefaultKey, label: "引擎默认" }] : modelOptions.map(m => ({ ...m, value: modelKey(modelSelection(m.value).modelId, m.providerId) }))}
          emptyNote={catalogError || "读取模型…"}
          onSelect={value => {
            if (value === engineDefaultKey) { set({ modelId: null, providerId: null }); return; }
            const [providerId, modelId] = JSON.parse(value); const next = models.find(m => modelKey(m.modelId, m.providerId) === value);
            set({ modelId, providerId, reasoningMode: next?.reasoningLevels.includes(draft.reasoningMode ?? "") ? draft.reasoningMode : null });
          }} /></CoordinatorField>
        {effective?.conversationControls?.reasoning && reasoning.length > 0 && <CoordinatorField label="推理强度"><BarMenu floating label="推理强度" value={draft.reasoningMode ?? null}
          display={draft.reasoningMode ?? "默认推理强度"} options={[{ value: "", label: "默认" }, ...reasoning.map(value => ({ value, label: value }))]}
          onSelect={reasoningMode => set({ reasoningMode: reasoningMode || null })} /></CoordinatorField>}
        {approval.length > 0 && <CoordinatorField label="权限"><BarMenu floating label="权限" value={draft.approvalMode ?? null} display={draft.approvalMode ? approvalLabel(draft.approvalMode, t, effective?.conversationControls?.approvalOptions) : "默认权限"}
          options={[{ value: "", label: "默认" }, ...approval.map(value => ({ value, label: approvalLabel(value, t, effective?.conversationControls?.approvalOptions), title: effective?.conversationControls?.approvalOptions?.find(option => option.id === value)?.description }))]}
          onSelect={approvalMode => set({ approvalMode: approvalMode || null })} /></CoordinatorField>}
        {execution.length > 0 && <CoordinatorField label={t("bar.execution")}><BarMenu floating label={t("bar.execution")} value={draft.executionMode ?? null}
          display={execution.find(option => option.id === draft.executionMode)?.name ?? "默认模式"}
          options={[{ value: "", label: "默认" }, ...execution.map(option => ({ value: option.id, label: option.name }))]}
          onSelect={executionMode => set({ executionMode: executionMode || null })} /></CoordinatorField>}
        </div>
        <button type="button" className="kaus-coordinator-more" onClick={() => setMore(!more)} aria-expanded={more}><ChevronDown size={13} aria-hidden className={more ? "is-expanded" : ""} />更多设置</button>
        {more && <div className="kaus-coordinator-fields">
          {!!effective?.conversationControls?.configOptions?.length && <div className="is-wide"><EngineConfigFields
            options={effective.conversationControls.configOptions} values={draft.configOptions}
            onChange={!busy ? (key, value) => set({ configOptions: { ...draft.configOptions, [key]: value } }) : undefined} /></div>}
          {bindings.find(b => b.id === draft.bindingId) && <a className="kaus-coordinator-project-link" href={projectPath(bindings.find(b => b.id === draft.bindingId)!.projectId)} target="_blank" rel="noreferrer">插件与规则<ArrowUpRight size={13} aria-hidden /></a>}
          <div className="is-wide"><WorkspacePill setting={{ value: draft.workspaceRoot ?? effective?.workspaceRoot.value ?? null, source: draft.workspaceRoot ? "binding" : "project" }} onSave={async workspaceRoot => { set({ workspaceRoot }); }} /></div>
          <label className="is-wide">主持要求<textarea style={inputStyle()} rows={4} value={draft.instructions ?? ""} onChange={e => set({ instructions: e.target.value })} /></label>
          <label>调用上限<input style={inputStyle()} type="number" min={2} max={200} value={draft.callCap ?? 40} onChange={e => set({ callCap: Number(e.target.value) })} /></label>
          <label>运行时限（分钟）<input style={inputStyle()} type="number" min={1} max={240} value={draft.timeLimitMinutes ?? 30} onChange={e => set({ timeLimitMinutes: Number(e.target.value) })} /></label>
        </div>}
        {running && <span className="kaus-group-dim">下次任务生效</span>}
        {(error || catalogError) && <ErrorNotice error={error || catalogError} className="kaus-group-error" />}
        <div className="kaus-coordinator-actions">
          {!global && defaults && bindings.some(binding => binding.id === defaults.bindingId && engineAcceptsNewWork(binding)) && <Button onClick={() => { const source = bindings.find(b => b.id === defaults.bindingId); if (source) setProjectId(source.projectId); setDraft({ ...defaults, revision: Math.max(1, revision), sourceRevision: defaults.revision }); }}>应用默认</Button>}
          {!global && <Button disabled={busy || loading || !draft.bindingId || !catalogReady || !engineAcceptsNewWork(selectedBinding)} onClick={() => void save(true)}>设为默认</Button>}
          {running && <Button disabled={busy || loading || !draft.bindingId || !catalogReady} onClick={() => void save(false, true)}>停止并接管</Button>}
          <Button disabled={busy || loading || !draft.bindingId || !catalogReady || (global && !engineAcceptsNewWork(selectedBinding))} onClick={() => void save()}>{busy ? "保存中…" : "保存"}</Button>
        </div>
      </div>
    </Modal>}
  </>;
}
