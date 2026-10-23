import { ChevronDown, RefreshCw } from "lucide-react";
import { useLocale } from "../i18n";
import { backendDisplay } from "../lib/backend-display";
import { modelOptionsFor, modelSelection, modelValue } from "../lib/modelCatalogView";
import { useWorkbenchRuntime } from "../hooks/useWorkbenchRuntime";

export function WorkbenchRuntimeFields({ runtime, disabled = false }: { runtime: ReturnType<typeof useWorkbenchRuntime>; disabled?: boolean }) {
  const { t } = useLocale();
  const { value, binding, catalog, choices, connections, projects } = runtime;
  const options = modelOptionsFor(catalog, binding);
  const labelCounts = new Map<string, number>();
  for (const option of options) labelCounts.set(option.label, (labelCounts.get(option.label) ?? 0) + 1);
  const selectedModel = modelValue(value.modelId, value.providerId, catalog) ?? "";
  const locked = disabled || runtime.loading || runtime.busy;
  return <>
    <label>{t("workbench.project")}<span className="workbench-runtime-select"><select aria-label={t("workbench.project")} value={value.projectId ?? ""} disabled={locked} onChange={event => runtime.selectProject(event.target.value)}><option value="">{t("workbench.select")}</option>{projects.map(item => <option key={item.id} value={item.id}>{item.displayName || item.slug}</option>)}</select><ChevronDown size={14} aria-hidden="true"/></span></label>
    <label>{t("workbench.agent")}<span className="workbench-runtime-select"><select aria-label={t("workbench.agent")} value={value.bindingId ?? ""} disabled={locked || !value.projectId || runtime.bindingsLoading} onChange={event => void runtime.selectEngine(event.target.value)}><option value="">{t("workbench.select")}</option>{choices.map(item => <option key={item.value} value={item.value}>{backendDisplay(item.backendId, connections.find(connection => connection.id === item.backendId)?.displayName).displayName}</option>)}</select><ChevronDown size={14} aria-hidden="true"/></span></label>
    {binding && <label>{t("workbench.model")}<span className="workbench-runtime-select"><select aria-label={t("workbench.model")} value={selectedModel} disabled={locked} onChange={event => { const selection = modelSelection(event.target.value); runtime.selectModel(selection.modelId, selection.providerId ?? null); }}><option value="">{t("workbench.default")}</option>{options.map(item => <option key={item.value} value={item.value}>{(labelCounts.get(item.label) ?? 0) > 1 && (item.group || item.providerId) ? `${item.label} · ${item.group || item.providerId}` : item.label}</option>)}{value.modelId && !options.some(item => item.value === selectedModel) && <option value={selectedModel}>{value.modelId}</option>}</select><ChevronDown size={14} aria-hidden="true"/></span></label>}
    {runtime.failed && <div className="workbench-inline-error" role="alert"><span>{t("workbench.loadFailed")}</span><button className="kaus-chat-icon-action" title={t("workbench.retry")} aria-label={t("workbench.retry")} onClick={runtime.reload}><RefreshCw size={15}/></button></div>}
  </>;
}
