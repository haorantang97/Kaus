import { engineAcceptsNewWork } from "./engineAvailability";
import { engineFamily } from "./backend-display";
import { preferredEngineConnections } from "./enginePresets";
import type { BackendWire, BindingWire } from "./sessionApi";

/** Imported rows can point at a backend with no running driver; a connected one of the same product wins. */
export function bindingConnected(binding: BindingWire, connections: BackendWire[]) {
  return connections.some(connection => connection.id === binding.backendId);
}

/** Default pick for a project: explicit id, then a connected default, then any connected, then the old order. */
export function preferredBinding(usable: BindingWire[], connections: BackendWire[], current?: string | null) {
  const connected = (binding: BindingWire) => bindingConnected(binding, connections);
  return usable.find(b => b.id === current) ?? usable.find(b => b.isDefault && connected(b)) ?? usable.find(connected)
    ?? usable.find(b => b.isDefault) ?? usable[0];
}

/** One product per menu, with project/persona associations kept behind it. */
export function engineSelectionChoices(bindings: BindingWire[], connections: BackendWire[], selected?: string | null, remembered: string[] = []) {
  bindings = bindings.filter(binding => engineAcceptsNewWork(binding) && engineAcceptsNewWork(connections.find(connection => connection.id === binding.backendId)));
  connections = connections.filter(engineAcceptsNewWork);
  const groups = new Map<string, BindingWire>();
  const rank = (b: BindingWire) => Number(bindingConnected(b, connections)) * 64 + Number(b.id === selected) * 32 + Number(remembered.includes(b.id)) * 16 + Number(!b.discriminator) * 4
    + Number(b.isDefault) * 2 + Number(connections.find(c => c.id === b.backendId)?.preferred ?? false);
  for (const binding of bindings) {
    if (!binding.enabled) continue;
    const product = connections.find(c => c.id === binding.backendId)?.productId ?? engineFamily(binding.backendId);
    const current = groups.get(product);
    if (!current || rank(binding) > rank(current)) groups.set(product, binding);
  }
  const linked = new Set(bindings.filter(b => b.enabled).map(b => b.backendId));
  return [
    ...[...groups].map(([productId, binding]) => ({ value: binding.id, backendId: binding.backendId, productId })),
    ...preferredEngineConnections(connections.filter(c => !linked.has(c.id) && !groups.has(c.productId ?? engineFamily(c.id))))
      .map(c => ({ value: c.id, backendId: c.id, productId: c.productId ?? engineFamily(c.id) })),
  ];
}
