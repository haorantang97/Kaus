import { engineAcceptsNewWork } from "../lib/engineAvailability";
import { useEffect, useRef, useState } from "react";
import { BarMenu } from "./ComposerBar";
import { EngineIcon } from "./EngineIcon";
import { backendDisplay, engineBrand } from "../lib/backend-display";
import { engineSelectionChoices } from "../lib/engineSelection";
import { describeFailure, ensureProjectBinding, fetchConnectedBackends, fetchProjectBindings,
  type BackendWire, type BindingWire } from "../lib/sessionApi";
import { useLocale } from "../i18n";

export function ProjectAgentPicker({ projectId, value, onChange, allowedBackendIds, retainedBinding, disabled = false }: {
  projectId: string; value?: string | null; onChange: (binding: BindingWire | null) => void;
  allowedBackendIds?: string[]; retainedBinding?: BindingWire; disabled?: boolean;
}) {
  const { t } = useLocale();
  const [bindings, setBindings] = useState<BindingWire[]>([]);
  const [connections, setConnections] = useState<BackendWire[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const epoch = useRef(0);
  const changed = useRef(onChange); changed.current = onChange;
  const retained = useRef(retainedBinding); retained.current = retainedBinding;
  const currentValue = useRef(value); currentValue.current = value;
  const allowedKey = allowedBackendIds?.join("\0");
  useEffect(() => {
    const generation = ++epoch.current;
    const controller = new AbortController();
    setLoading(true); setError(""); setBindings([]);
    if (!projectId) { setLoading(false); return; }
    Promise.all([fetchConnectedBackends(controller.signal), fetchProjectBindings(projectId, controller.signal, { includeStatus: false })])
      .then(([global, local]) => {
        if (generation !== epoch.current) return;
        setConnections(global.backends); setBindings(local.bindings);
        const usable = local.bindings.filter(b => b.enabled && engineAcceptsNewWork(b) && engineAcceptsNewWork(global.backends.find(c => c.id === b.backendId)) && (!allowedBackendIds || allowedBackendIds.includes(b.backendId)));
        const selected = usable.find(b => b.id === currentValue.current) ?? usable.find(b => b.isDefault) ?? usable[0];
        if (retained.current?.id === currentValue.current && retained.current?.projectId === projectId) return;
        if ((selected?.id ?? null) !== (currentValue.current ?? null)) changed.current(selected ?? null);
      }).catch(e => { if (!controller.signal.aborted) setError(describeFailure(e)); })
      .finally(() => { if (generation === epoch.current) setLoading(false); });
    return () => { controller.abort(); epoch.current += 1; };
  }, [projectId, allowedKey]);
  const select = async (choice: string) => {
    const generation = ++epoch.current;
    const existing = bindings.find(b => b.id === choice);
    if (existing) { changed.current(existing); return; }
    setLoading(true); setError(""); changed.current(null);
    try {
      const binding = await ensureProjectBinding(projectId, choice);
      if (generation !== epoch.current) return;
      setBindings(list => [...list.filter(b => b.id !== binding.id), binding]);
      changed.current(binding);
    } catch (e) { if (generation === epoch.current) setError(describeFailure(e)); }
    finally { if (generation === epoch.current) setLoading(false); }
  };
  const choices = engineSelectionChoices(
    bindings.filter(b => !allowedBackendIds || allowedBackendIds.includes(b.backendId)),
    connections.filter(c => !allowedBackendIds || allowedBackendIds.includes(c.id)), value);
  const selected = choices.find(c => c.value === value) ?? (retainedBinding && retainedBinding.id === value && retainedBinding.projectId === projectId ? { backendId:retainedBinding.backendId, productId:retainedBinding.backendId } : undefined);
  const label = (id: string) => {
    const connection = connections.find(c => c.id === id);
    return engineBrand(connection?.productId ?? id)?.name ?? connection?.productName ?? backendDisplay(id).displayName;
  };
  return <div>
    <BarMenu floating label={t("bar.engine")} value={value ?? null} disabled={disabled || loading}
      display={selected ? label(selected.backendId) : t("draft.engine.none")}
      icon={<EngineIcon engine={selected?.productId} />}
      options={choices.map(c => ({ value: c.value, label: label(c.backendId), icon: <EngineIcon engine={c.productId} /> }))}
      emptyNote={loading ? t("ui.loading") : t("engines.connect.empty")}
      onSelect={choice => void select(choice)} />
    {error && <div role="alert" className="kaus-group-error">{error}</div>}
  </div>;
}
