import { useEffect, useState } from "react";
import { fetchProjectBindings, fetchModelCatalog, type ModelCatalogWire, type BindingWire } from "./sessionApi";
import { modelOptionsFor, modelSelection } from "./modelCatalogView";

/** Compatibility profile screens consume the same binding catalog as the workbench. */
export function useProfileModelCatalog(name: string, revision: unknown) {
  const [state, setState] = useState<{ name: string; binding: BindingWire; catalog: ModelCatalogWire } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setState(null);
    fetchProjectBindings(`project:${name}`, controller.signal).then(async ({ bindings }) => {
      const binding = bindings.find(item => item.nativeScopeRef === name && item.backendId === "backend:hermes");
      if (!binding) return;
      const catalog = await fetchModelCatalog(binding.backendId, binding.id, controller.signal);
      if (!controller.signal.aborted) setState({ name, binding, catalog });
    }).catch(() => { /* Preserve the current value if discovery is unavailable. */ });
    return () => controller.abort();
  }, [name, revision]);
  const own = state?.name === name ? state : null;
  const options = modelOptionsFor(own?.catalog, own?.binding);
  const payloadFor = (value: string) => {
    const option = options.find(item => item.value === value);
    if (!option) return null;
    return { default: modelSelection(value).modelId, provider: option.providerId ?? null };
  };
  return { catalog: own?.catalog, options, payloadFor };
}
