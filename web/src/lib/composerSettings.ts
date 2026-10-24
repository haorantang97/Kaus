import type { UiCapabilities } from "../components/cards/capabilities";
import { modelEffort } from "./modelCatalogView";
import {
  emptyEffectiveSettings, fetchBackendUiCapabilities, fetchEffectiveSettings, fetchModelCatalog,
  type BindingWire, type EffectiveSettingsWire, type ModelCatalogWire, type ModelDescriptorWire,
} from "./sessionApi";

export interface ComposerSettings {
  bindingId: string;
  catalog: ModelCatalogWire | null;
  settings: EffectiveSettingsWire;
  caps: UiCapabilities | null;
  retryable: boolean;
}

/** One snapshot for both draft and existing conversations; old hosts keep their fallback. */
export async function readComposerSettings(binding: BindingWire, conversationId?: string, modelId?: string): Promise<ComposerSettings> {
  let retryable = false;
  const settings = await fetchEffectiveSettings(binding.id, undefined, {
    includeCatalog: true, ...(conversationId ? { conversationId } : {}), ...(modelId ? { modelId } : {}),
  }).catch(() => { retryable = true; return emptyEffectiveSettings(binding.id); });
  const [catalog, caps] = await Promise.all([
    settings.modelCatalog !== undefined ? settings.modelCatalog : (conversationId
      ? fetchModelCatalog(binding.backendId, binding.id, undefined, conversationId) : fetchModelCatalog(binding.backendId, binding.id))
      .catch(() => { retryable = true; return null; }),
    settings.uiCapabilities !== undefined ? settings.uiCapabilities : fetchBackendUiCapabilities(binding.backendId)
      .catch(() => { retryable = true; return null; }),
  ]);
  const ownCatalog = catalog?.bindingId === binding.id ? catalog : null;
  // A failed discovery is not a permanent capability verdict. Do not cache it as one.
  retryable ||= Boolean(catalog?.degraded || !caps || (!ownCatalog && caps.models.conversationScoped === "supported"));
  return { bindingId: binding.id, catalog: ownCatalog, caps, retryable, settings: {
    ...settings,
    model: settings.model.value ? settings.model : ownCatalog?.defaultModelId
      ? { value: ownCatalog.defaultModelId, source: "catalog" } : settings.model,
  } };
}

/** Bounded recovery, then recheck when the user returns. Never poll or start model work. */
export function observeComposerSettings(
  binding: BindingWire,
  onChange: (next: ComposerSettings) => void,
  { conversationId, modelId, initial }: { conversationId?: string; modelId?: string; initial?: ComposerSettings } = {},
): () => void {
  let disposed = false;
  let pending = false;
  let attempts = 0;
  let lastRead = initial ? Date.now() : 0;
  let retryable = initial?.retryable ?? true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const read = async () => {
    if (disposed || pending) return;
    clearTimeout(timer);
    pending = true;
    const next = await readComposerSettings(binding, conversationId, modelId);
    pending = false;
    if (disposed) return;
    lastRead = Date.now();
    retryable = next.retryable;
    onChange(next);
    if (retryable && attempts < 2) timer = setTimeout(() => { attempts += 1; void read(); }, attempts === 0 ? 1_500 : 5_000);
  };
  const refresh = () => {
    if (document.visibilityState === "hidden" || pending || (!retryable && Date.now() - lastRead < 30_000)) return;
    attempts = 0;
    void read();
  };
  window.addEventListener("focus", refresh);
  window.addEventListener("online", refresh);
  document.addEventListener("visibilitychange", refresh);
  if (!initial || initial.retryable) void read();
  return () => {
    disposed = true;
    clearTimeout(timer);
    window.removeEventListener("focus", refresh);
    window.removeEventListener("online", refresh);
    document.removeEventListener("visibilitychange", refresh);
  };
}

/** Defaults may arrive as session controls, model suffixes or persistent engine settings. */
export function composerReasoning(
  settings: EffectiveSettingsWire,
  model: ModelDescriptorWire | undefined,
  override?: string | null,
): EffectiveSettingsWire["reasoningEffort"] {
  const controls = settings.conversationControls;
  const levels = model?.reasoningLevels?.length ? model.reasoningLevels
    : controls?.reasoningLevels?.length ? controls.reasoningLevels : settings.reasoningEffort.levels;
  const value = override ?? modelEffort(model) ?? controls?.reasoningDefault ?? settings.reasoningEffort.value;
  return {
    value: value && levels.includes(value) ? value : null,
    source: override || modelEffort(model) || controls?.reasoningDefault || (controls?.reasoning && levels.length)
      ? "catalog" : settings.reasoningEffort.source,
    levels,
  };
}
