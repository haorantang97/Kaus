import { expect, it } from "vitest";
import { engineSelectionChoices, preferredBinding } from "./engineSelection";
import type { BackendWire, BindingWire } from "./sessionApi";

const binding = (id: string, backendId: string, extra: Partial<BindingWire> = {}) => ({
  id, projectId: "project:default", backendId, displayName: "default", nativeScopeRef: null, enabled: true,
  isDefault: false, defaultModelId: null, defaultProviderId: null, compatibilityState: "unknown", discriminator: null, ...extra,
}) as BindingWire;
const connected = [{ id: "backend:hermes-acp", productId: "hermes" }] as unknown as BackendWire[];
// An imported row for the same product whose backend has no registered driver.
const imported = binding("binding:default:hermes", "backend:hermes", { isDefault: true });
const working = binding("binding:default:hermes-acp", "backend:hermes-acp");

it("a connected binding wins over a default binding whose backend is not connected", () => {
  expect(engineSelectionChoices([imported, working], connected).map(choice => choice.value)).toEqual(["binding:default:hermes-acp"]);
  // Even when the unconnected row is the current selection.
  expect(engineSelectionChoices([imported, working], connected, imported.id).map(choice => choice.value)).toEqual(["binding:default:hermes-acp"]);
});

it("default pick prefers an explicit id, then connected bindings, then the old order", () => {
  expect(preferredBinding([imported, working], connected)?.id).toBe(working.id);
  expect(preferredBinding([imported, working], connected, imported.id)?.id).toBe(imported.id);
  // Mock and other unlisted engines keep their previous default when nothing is connected.
  expect(preferredBinding([imported, working], [])?.id).toBe(imported.id);
});
