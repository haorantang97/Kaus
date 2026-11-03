"""Driver-facing reads behind conversation settings: catalog and controls.

Four places ask a Driver "what can this Binding select right now": the
conversation ``PATCH`` (existing conversation), ``GET /backends/{id}/models``,
``GET /bindings/{id}/effective-settings`` and the group coordinator draft
(:meth:`app.api.group_coordinator.GroupCoordinator.validate_config`). They all
read the same two Driver surfaces, so the reads live here once:

- :func:`scoped_model_catalog`: the live session's catalog when the
  conversation has a runtime and the Driver offers ``runtime_model_catalog``,
  otherwise the Binding's cached / probed catalog (``get_model_catalog``);
- :func:`conversation_controls_of`: the Binding's ``conversation_controls``
  (``default`` when the Driver has none), overridden by
  ``runtime_conversation_controls`` for a live runtime.

What deliberately stays with each caller, because the semantics differ:

- **Error handling of the catalog read.** The PATCH turns only
  ``UnsupportedCapabilityError`` into "no catalog"; the models endpoint maps
  it to 501; effective-settings swallows every exception (the toolbar must
  render); the coordinator maps every exception to ``coordinator_unavailable``.
- **Model preview** (``preview_model_settings``). The PATCH previews only when
  no runtime is live *and* the requested model is in the catalog
  (``preview_valid``); effective-settings previews for any requested model
  without that guard; the coordinator previews after ``select_model`` validated
  the choice and rejects a degraded preview. Merging these would change which
  requests reach the engine.
- **Model selection** (:func:`drivers.model_selection.select_model`) and its
  error codes (``model_not_in_catalog`` / ``model_not_available`` /
  ``model_unavailable``) are per-endpoint contracts.
"""

from __future__ import annotations

from typing import Any

from app.projects.models import AgentBinding
from drivers.base import ModelCatalog, RuntimeHandle


async def scoped_model_catalog(
    driver: Any, binding: AgentBinding, handle: RuntimeHandle | None = None
) -> ModelCatalog:
    """Live-session catalog when a runtime exists and the Driver scopes it; else the Binding catalog."""
    scoped = getattr(driver, "runtime_model_catalog", None)
    if handle is not None and callable(scoped):
        return await scoped(handle, binding)
    return await driver.get_model_catalog(binding)


def conversation_controls_of(
    driver: Any,
    binding: AgentBinding,
    handle: RuntimeHandle | None = None,
    *,
    default: dict[str, Any],
) -> dict[str, Any]:
    """Binding-level controls (``default`` when the Driver has none), scoped to the live runtime if any."""
    reader = getattr(driver, "conversation_controls", None)
    controls = reader(binding) if callable(reader) else default
    scoped = getattr(driver, "runtime_conversation_controls", None)
    if handle is not None and callable(scoped):
        controls = scoped(handle, binding)
    return controls


__all__ = ["conversation_controls_of", "scoped_model_catalog"]
