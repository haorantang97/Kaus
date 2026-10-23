"""On-demand account quota snapshots, independent of conversations and Projects."""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone

from drivers.account_quota import AccountQuota, QuotaIssue, QuotaReadResult, QuotaUnavailable, quota_reader
from drivers.identity import product_identity
from drivers.explicit_quota.catalog import ConnectionInvalid

PUBLIC_REASONS = frozenset({
    'request_failed', 'provider_not_configured', 'credential_unavailable', 'credential_expired',
    'sign_in_required', 'management_key_required', 'cloud_credentials_required', 'service_not_running',
    'native_version_unsupported', 'no_usage_api', 'unsupported_provider', 'endpoint_retired',
    'web_session_required', 'quota_not_returned', 'unsupported_auth', 'permission_required', 'credential_access_required', 'dependency_missing', 'invalid_endpoint',
})


class AccountQuotaService:
    def __init__(self, registry, *, reader_factory=quota_reader, ttl=60, refresh_interval=30, timeout=40, clock=time.monotonic, connections=None):
        self.registry = registry
        self.connections = connections
        self.reader_factory = reader_factory
        self.ttl = ttl
        self.timeout = timeout
        self.refresh_interval = refresh_interval
        self.clock = clock
        self._cache = None
        self._cached_at = float('-inf')
        self._drivers = ()
        self._lock = asyncio.Lock()

    async def snapshot(self, *, force=False):
        requested_at = self.clock()
        async with self._lock:
            drivers = tuple(d for d in self.registry.list_drivers() if d.driver_kind != 'mock')
            identity = (tuple((d.backend_id, id(d), id(getattr(d, 'agent_spec', None))) for d in drivers),
                        self.connections.revision if self.connections else None)
            if (self._cache is not None and identity == self._drivers
                    and self.clock() - self._cached_at < self.ttl
                    and (not force or self._cached_at >= requested_at
                         or self.clock() - self._cached_at < self.refresh_interval)):
                return self._cache
            checked = datetime.now(timezone.utc).isoformat()
            explicit_task = asyncio.create_task(self.connections.read_accounts(with_versions=True)) if self.connections else None
            try:
                results = await asyncio.gather(*(self._read(driver) for driver in drivers))
                if self.connections:
                    self.connections.register_targets(issue for result in results for issue in result.issues)
                explicit = await explicit_task if explicit_task else []
            finally:
                if explicit_task and not explicit_task.done():
                    explicit_task.cancel()
                    await asyncio.gather(explicit_task, return_exceptions=True)
            records, native_sources = [], {}
            for driver, result in zip(drivers, results):
                preset = getattr(driver, 'preset', None)
                product = getattr(preset, 'id', None) or getattr(driver, 'product_id', None) or driver.backend_id
                label = product_identity(product, getattr(preset, 'label', None) or 'Agent')['productName']
                records.append(([driver.backend_id], None, None, label, result))
                for item in (*result.accounts, *result.issues):
                    if item.source_id:
                        source = native_sources.setdefault(item.source_id, {'agentIds': [], 'available': False})
                        if driver.backend_id not in source['agentIds']: source['agentIds'].append(driver.backend_id)
                        source['available'] |= isinstance(item, AccountQuota)
            try:
                connection_records = {row['id']: row for row in self.connections.store.all()} if self.connections else {}
            except ConnectionInvalid:
                connection_records = {}
            replacements, native_connections = set(), {}
            for identifier, label, result, version in explicit:
                record = connection_records.get(identifier)
                # A removed connection must not reappear from an in-flight refresh.
                if not record and identifier != 'connections': continue
                if record and record['version'] != version:
                    # Another request edited credentials after their read finished
                    # while a slower native reader was still in flight.
                    result = QuotaReadResult(issues=(QuotaIssue('unavailable', reason='request_failed'),))
                    label = record['label']
                source_id = record.get('sourceId') if record else None
                native = native_sources.get(source_id)
                if native and native['available']:
                    native_connections.setdefault(source_id, []).append(identifier)
                    continue
                if native: replacements.add(source_id)
                records.append((native['agentIds'] if native else [], identifier, source_id, label, result))
            accounts, unavailable = {}, []
            issue_keys = set()
            for agent_ids, connection_id, bound_source, label, result in records:
                for issue in result.issues:
                    source_id = bound_source or issue.source_id
                    if not connection_id and source_id in replacements: continue
                    if not connection_id and source_id and native_sources.get(source_id, {}).get('available'): continue
                    issue_key = (source_id or connection_id or tuple(agent_ids), issue.provider, issue.state, issue.reason)
                    if issue_key in issue_keys: continue
                    issue_keys.add(issue_key)
                    row = {'label': issue.provider or label, 'state': issue.state}
                    if agent_ids: row['agentId'] = agent_ids[0]
                    if connection_id: row['connectionId'] = connection_id
                    if source_id: row['sourceId'] = source_id
                    if issue.provider: row['provider'] = issue.provider
                    if issue.reason in PUBLIC_REASONS: row['reason'] = issue.reason
                    if self.connections:
                        recovery = self.connections.recovery(source_id, connection_records.get(connection_id))
                        if recovery: row['recovery'] = recovery
                    unavailable.append(row)
                for index, quota in enumerate(result.accounts):
                    # Only a proven account/key scope permits cross-connection grouping.
                    source = quota.account_key or f'{agent_ids or connection_id}:{index}'
                    key = hashlib.sha256(f'{quota.provider}:{source}'.encode()).hexdigest()[:24]
                    connection_ids = [connection_id] if connection_id else native_connections.get(quota.source_id, [])
                    if key in accounts:
                        if accounts[key]['kind'] != quota.kind: accounts[key]['kind'] = 'mixed'
                        for agent_id in agent_ids:
                            if agent_id not in accounts[key]['agentIds']: accounts[key]['agentIds'].append(agent_id)
                        for identifier in connection_ids:
                            if identifier not in accounts[key].setdefault('connectionIds', []):
                                accounts[key]['connectionIds'].append(identifier)
                        for field, values in [('windows', quota.windows), ('balances', quota.balances)]:
                            existing = {value['id'] for value in accounts[key][field]}
                            for value in values:
                                if value.id not in existing:
                                    accounts[key][field].append(value.to_wire()); existing.add(value.id)
                        continue
                    accounts[key] = {'id': key, 'provider': quota.provider, 'accountLabel': quota.account_label,
                        'plan': quota.plan, 'agentIds': list(agent_ids), 'status': 'available', 'kind': quota.kind,
                        'windows': [w.to_wire() for w in quota.windows], 'balances': [b.to_wire() for b in quota.balances], 'updatedAt': checked}
                    if connection_ids: accounts[key]['connectionIds'] = list(connection_ids)
            display_rows = [(row, 'accountLabel') for row in accounts.values()] + [(row, 'label') for row in unavailable]
            labels = [row[field] for row, field in display_rows]
            counts = {}
            for row, field in display_rows:
                if labels.count(row[field]) > 1:
                    base = row[field]
                    counts[base] = counts.get(base, 0) + 1
                    row[field] += f' · 账号 {counts[base]}'
            self._cache = {'accounts': list(accounts.values()), 'unavailable': unavailable, 'updatedAt': checked}
            self._drivers = identity
            self._cached_at = self.clock()
            return self._cache

    async def _read(self, driver):
        try:
            reader = self.reader_factory(driver)
            if reader is None:
                return QuotaReadResult(issues=(QuotaIssue('unsupported'),))
            result = await asyncio.wait_for(reader(), timeout=self.timeout)
            if isinstance(result, AccountQuota): return QuotaReadResult((result,))
            if isinstance(result, QuotaReadResult): return result
            return QuotaReadResult(issues=(QuotaIssue('unavailable'),))
        except QuotaUnavailable as exc:
            return QuotaReadResult(issues=(QuotaIssue(exc.state, reason=exc.reason),))
        except Exception:
            # Account discovery must not expose paths, credentials or native errors.
            return QuotaReadResult(issues=(QuotaIssue('unavailable'),))
