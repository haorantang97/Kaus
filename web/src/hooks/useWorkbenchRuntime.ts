import { useEffect, useRef, useState } from "react";
import { engineSelectionChoices } from "../lib/engineSelection";
import { fetchProjects, fetchProjectBindings, fetchConnectedBackends, ensureProjectBinding, fetchModelCatalog, type ProjectWire, type BindingWire, type BackendWire, type ModelCatalogWire } from "../lib/sessionApi";
import type { ModuleConfig } from "../lib/workbenchApi";

export type WorkbenchRuntimeConfig = Omit<ModuleConfig, "grants">;
/** One shared selection path for module settings and local-project onboarding. */
export function useWorkbenchRuntime(initial: Partial<WorkbenchRuntimeConfig> = {}) {
  const [projects, setProjects] = useState<ProjectWire[]>([]);
  const [connections, setConnections] = useState<BackendWire[]>([]);
  const [bindings, setBindings] = useState<BindingWire[]>([]);
  const [projectId, setProjectId] = useState(initial.projectId ?? "");
  const [bindingId, setBindingId] = useState(initial.bindingId ?? "");
  const [modelId, setModelId] = useState(initial.modelId ?? "");
  const [providerId, setProviderId] = useState(initial.providerId ?? null);
  const [catalog, setCatalog] = useState<ModelCatalogWire | null>(null);
  const [loading, setLoading] = useState(true);
  const [bindingsLoading, setBindingsLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const generation = useRef(0);
  const binding = bindings.find(item => item.id === bindingId);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setFailed(false);
    void Promise.all([fetchProjects(controller.signal), fetchConnectedBackends(controller.signal)])
      .then(([p, c]) => { if (!Array.isArray(p.projects) || !Array.isArray(c.backends)) throw new Error("invalid_runtime_catalog"); if (!controller.signal.aborted) { setProjects(p.projects.filter(item => item.status !== "archived")); setConnections(c.backends); } })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    setBindings([]); setCatalog(null);
    if (!projectId) { setBindingsLoading(false); return; }
    const controller = new AbortController();
    setBindingsLoading(true);
    void fetchProjectBindings(projectId, controller.signal, { includeStatus: false })
      .then(data => { if (!Array.isArray(data.bindings)) throw new Error("invalid_bindings"); if (!controller.signal.aborted) setBindings(data.bindings); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setBindingsLoading(false); });
    return () => controller.abort();
  }, [projectId, revision]);
  useEffect(() => {
    setCatalog(null);
    if (!binding) return;
    const controller = new AbortController();
    void fetchModelCatalog(binding.backendId, binding.id, controller.signal)
      .then(data => { if (!controller.signal.aborted) setCatalog(data); }).catch(() => {});
    return () => controller.abort();
  }, [binding]);
  useEffect(() => () => { generation.current++; }, []);
  const choices = engineSelectionChoices(bindings, connections, bindingId);
  function selectProject(value: string) {
    generation.current++; setBusy(false); setFailed(false);
    setProjectId(value); setBindingId(""); setModelId(""); setProviderId(null);
  }
  async function selectEngine(value: string) {
    const choice = choices.find(item => item.value === value);
    if (!choice) return;
    const current = ++generation.current;
    setBusy(true); setFailed(false); setModelId(""); setProviderId(null);
    try {
      const selected = bindings.find(item => item.id === value) ?? await ensureProjectBinding(projectId, choice.backendId);
      if (current !== generation.current) return;
      setBindings(previous => previous.some(item => item.id === selected.id) ? previous : [...previous, selected]);
      setBindingId(selected.id);
    } catch { if (current === generation.current) setFailed(true); }
    finally { if (current === generation.current) setBusy(false); }
  }
  const value: WorkbenchRuntimeConfig = { projectId: projectId || null, bindingId: bindingId || null, modelId: modelId || null, providerId, reasoningMode: initial.reasoningMode ?? null };
  return { projects, connections, binding, choices, catalog, value, loading, bindingsLoading, busy, failed,
    ready: Boolean(projectId && binding && !loading && !bindingsLoading && !busy && !failed),
    selectProject, selectEngine, selectModel: (model: string, provider: string | null) => { setModelId(model); setProviderId(provider); },
    reload: () => setRevision(value => value + 1) };
}
