"""Read-only computer-use setup summary for the local workbench."""
from __future__ import annotations

import asyncio
import re
import time
from copy import deepcopy
from collections.abc import Mapping

from drivers.computer_use import CAPABILITIES, discover_fallbacks
from drivers.computer_use.integration import reconcile_resources
from drivers.computer_use.probe import probe_fallbacks
from drivers.computer_use.permissions import native_setup, setup_details, open_permission_settings, reveal_permission_host
from drivers.identity import product_identity
from drivers.computer_use import settings as component_settings, installer


class ComputerUseSettings:
    def __init__(self, registry):
        self.registry = registry
        self._cache = None
        self._cached_at = 0.0

    async def set_enabled(self, enabled):
        component_settings.save_enabled(enabled)
        self._cached_at = 0.0
        if enabled:
            installer.start()
        # Disabling affects new executions only. Active owners retain their lease.
        return self._current_snapshot()

    async def retry_setup(self):
        if not component_settings.is_enabled():
            raise ValueError("not_enabled")
        installer.start()
        self._cached_at = 0.0
        return self._current_snapshot()

    async def request_permissions(self):
        if not component_settings.is_enabled():
            raise ValueError("not_enabled")
        return await installer.request_permissions()

    async def verify_capture(self):
        if not component_settings.is_enabled():
            raise ValueError("not_enabled")
        return await installer.request_permissions(direct_capture=True)

    async def permission_status(self):
        """Refresh the setup component without polling every installed Agent."""
        if not component_settings.is_enabled() or installer.snapshot()["status"] in installer.BUSY:
            return self._current_snapshot()
        previous = next((row for row in (self._cache or {}).get("fallbacks", [])
                         if row.get("provider") == "cua-driver"), {})
        if not previous or {"accessibility", "screen-recording"}.intersection(previous.get("requirements", [])):
            await installer.refresh_permission_host()
        candidates = await probe_fallbacks(capabilities=["desktop"], force=True)
        if self._cache is None:
            self._cache = {"agents": [], "fallbacks": [], "pendingResources": []}
        rows = [row for row in self._cache["fallbacks"] if row.get("capability") != "desktop"]
        for candidate in candidates:
            row = {**candidate.to_dict(), "capability": candidate.capability}
            if setup := setup_details(candidate.provider_id, candidate.requirements):
                row["permissionSetup"] = setup
            rows.append(row)
            if candidate.provider_id == "cua-driver":
                installer.mark_readiness(candidate.status == "ready", permission_required=bool(
                    {"screen-recording", "accessibility"}.intersection(candidate.requirements)))
        self._cache["fallbacks"] = rows
        component_settings.record_runtime_readiness(self._cache["agents"], rows)
        return self._current_snapshot()

    def _current_snapshot(self):
        value = deepcopy(self._cache) if self._cache is not None else {
            "agents": [], "fallbacks": [], "pendingResources": [],
        }
        value.update(enabled=component_settings.is_enabled(), setup=installer.snapshot())
        cua = next((row for row in value["fallbacks"] if row.get("provider") == "cua-driver"), {})
        if cua.get("systemPermissions"):
            value["setup"]["permissions"] = dict(cua["systemPermissions"])
        hosts = set()
        for driver in self.registry.list_drivers():
            if (getattr(driver, "driver_kind", None) == "mock" or
                    getattr(driver, "native_computer_use_permission_control", None) != "agent"):
                continue
            preset = getattr(driver, "preset", None)
            product = getattr(preset, "id", None) or getattr(driver, "product_id", None) or driver.backend_id
            hosts.add(product_identity(product, getattr(preset, "label", None) or "Agent")["productName"])
        value["nativePermissionHosts"] = sorted(hosts)
        return value

    async def open_permission_settings(self, permission):
        return await open_permission_settings(permission)

    async def reveal_permission_host(self, host):
        if host == "cua-driver" and (bundle := component_settings.managed_bundle()):
            from drivers.computer_use.permissions import _open
            return await _open("-R", str(bundle))
        return await reveal_permission_host(host)

    async def snapshot(self, *, force=False):
        if not component_settings.is_enabled():
            # Turning the feature off stops optional discovery, not ownership
            # reconciliation for an already-running or quarantined action.
            pending = await reconcile_resources()
            value = self._current_snapshot()
            value["pendingResources"] = list(pending)
            return value
        state = installer.snapshot()
        if state["status"] in installer.BUSY or (
            not force and self._cache is not None and time.monotonic() - self._cached_at < 60
        ):
            return self._current_snapshot()
        async def read(driver):
            probe = getattr(driver, "computer_use_status", None)
            if probe is None:
                return None
            try:
                result = await asyncio.wait_for(probe(), timeout=15)
            except Exception:
                result = {}
            preset = getattr(driver, "preset", None)
            product = getattr(preset, "id", None) or getattr(driver, "product_id", None) or driver.backend_id
            identity = product_identity(product, getattr(preset, "label", None) or "Agent")
            label = identity["productName"]
            result = result if isinstance(result, Mapping) else {}
            capabilities = {}
            for cap in CAPABILITIES:
                raw = result.get(cap, {})
                raw = raw if isinstance(raw, Mapping) else {}
                status = raw.get("status")
                requirements = raw.get("requirements")
                requirements = requirements if isinstance(requirements, (list, tuple)) else ()
                capabilities[cap] = {
                    "status": status if isinstance(status, str) and status in {
                        "ready", "unknown", "needs_setup", "unavailable", "disabled", "denied"
                    } else "unknown",
                    "requirements": [value for value in requirements if isinstance(value, str)
                                     and re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value)],
                }
                if setup := native_setup(raw):
                    capabilities[cap]["permissionSetup"] = setup
            return {"id": identity["productId"], "label": label, **capabilities}
        drivers = [driver for driver in self.registry.list_drivers() if driver.driver_kind != "mock"]
        rows, fallbacks, pending = await asyncio.gather(
            asyncio.gather(*(read(driver) for driver in drivers)),
            probe_fallbacks(force=force),
            reconcile_resources(),
        )
        # Missing providers are setup items, not conversation errors.
        discovered = fallbacks or discover_fallbacks()
        products = {}
        for row in rows:
            if row is None:
                continue
            current = products.get(row["id"])
            if current is None:
                products[row["id"]] = row
                continue
            for cap in CAPABILITIES:
                if current[cap] != row[cap]:
                    current[cap] = {"status": "unknown", "requirements": ["connection_specific"]}
        fallback_rows = []
        for candidate in discovered:
            row = {**candidate.to_dict(), "capability": candidate.capability}
            if setup := setup_details(candidate.provider_id, candidate.requirements):
                row["permissionSetup"] = setup
            fallback_rows.append(row)
        self._cache = {
            "agents": list(products.values()),
            "fallbacks": fallback_rows,
            "pendingResources": list(pending),
        }
        self._cached_at = time.monotonic()
        component_settings.record_runtime_readiness(self._cache["agents"], fallback_rows)
        cua = next((candidate for candidate in discovered if candidate.provider_id == "cua-driver"), None)
        if cua is not None:
            installer.mark_readiness(cua.status == "ready", permission_required=bool(
                {"screen-recording", "accessibility"}.intersection(cua.requirements)))
        return self._current_snapshot()
