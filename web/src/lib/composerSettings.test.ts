import { afterEach, expect, it, vi } from "vitest";
import { ALL_SUPPORTED_UI_CAPABILITIES } from "../components/cards/capabilities";
import { composerReasoning, observeComposerSettings } from "./composerSettings";
import { emptyEffectiveSettings, fetchEffectiveSettings, type BindingWire, type EffectiveSettingsWire } from "./sessionApi";

vi.mock("./sessionApi", async () => ({
  ...await vi.importActual<typeof import("./sessionApi")>("./sessionApi"), fetchEffectiveSettings: vi.fn(),
}));
const binding = { id: "binding:test", backendId: "backend:test" } as BindingWire;
const missing = { ...emptyEffectiveSettings(binding.id), modelCatalog: null, uiCapabilities: ALL_SUPPORTED_UI_CAPABILITIES };
const ready: EffectiveSettingsWire = { ...missing, modelCatalog: {
  bindingId: binding.id, mode: "constrained", defaultModelId: "model-a", defaultProviderId: null, supportsReasoning: false,
  models: [{ modelId: "model-a", displayName: "Model A", providerId: null, contextWindow: null, reasoningLevels: [] }],
} };
afterEach(() => { vi.useRealTimers(); vi.resetAllMocks(); });

it("uses session-discovered effort options when a generic model has no embedded levels", () => {
  const settings = { ...ready, conversationControls: {
    reasoning: true, reasoningLevels: ["low", "high"], reasoningDefault: "high", approvalModes: [],
  } };
  expect(composerReasoning(settings, ready.modelCatalog!.models[0])).toMatchObject({
    value: "high", levels: ["low", "high"], source: "catalog",
  });
});

it("automatically recovers temporary discovery failure without requiring an engine switch", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchEffectiveSettings).mockResolvedValueOnce(missing).mockResolvedValue(ready);
  const update = vi.fn();
  const stop = observeComposerSettings(binding, update);
  try {
    await vi.advanceTimersByTimeAsync(1501);
    expect(update).toHaveBeenCalledTimes(2);
    expect(update.mock.lastCall?.[0].settings.model).toEqual({ value: "model-a", source: "catalog" });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchEffectiveSettings).toHaveBeenCalledTimes(2);
  } finally { stop(); }
});

it("bounds retries for persistent discovery failure and resumes on return", async () => {
  vi.useFakeTimers();
  vi.mocked(fetchEffectiveSettings).mockResolvedValue(missing);
  const stop = observeComposerSettings(binding, vi.fn());
  try {
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchEffectiveSettings).toHaveBeenCalledTimes(3);
    vi.mocked(fetchEffectiveSettings).mockResolvedValue(ready);
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchEffectiveSettings).toHaveBeenCalledTimes(4);
  } finally { stop(); }
});

it("discards a late result and its retries after changing conversation", async () => {
  vi.useFakeTimers();
  let finish!: (value: EffectiveSettingsWire) => void;
  vi.mocked(fetchEffectiveSettings).mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const update = vi.fn();
  const stop = observeComposerSettings(binding, update, { conversationId: "conversation:test" });
  stop();
  finish(missing);
  await vi.advanceTimersByTimeAsync(60_000);
  window.dispatchEvent(new Event("focus"));
  expect(update).not.toHaveBeenCalled();
  expect(fetchEffectiveSettings).toHaveBeenCalledTimes(1);
});
