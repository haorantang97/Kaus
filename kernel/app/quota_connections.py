"""Local billing connections: encrypted credentials, explicit validation and login."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import secrets as secure
from threading import RLock
import time
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from drivers.account_quota import AccountQuota, QuotaIssue, QuotaReadResult, QuotaUnavailable
from drivers.explicit_quota import catalog, validate, read_connection
from drivers.explicit_quota.catalog import ConnectionInvalid, SECRET_FIELDS, provider_spec


class ConnectionStore:
    def __init__(self, root):
        self.root = Path(root)
        self._mutex = RLock()

    @contextmanager
    def _locked(self):
        with self._mutex:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            fd = os.open(self.root / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _cipher(self):
        path = self.root / 'key'
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if path.is_symlink(): raise ConnectionInvalid('storage_unavailable')
        else:
            with os.fdopen(fd, 'wb') as f: f.write(Fernet.generate_key())
        return Fernet(path.read_bytes())

    def _read(self):
        path = self.root / 'connections.enc'
        if not path.exists(): return []
        try:
            if path.is_symlink() or path.stat().st_size > 8_000_000: raise ValueError()
            rows = json.loads(self._cipher().decrypt(path.read_bytes()))
            if not isinstance(rows, list): raise ValueError()
            return rows
        except (ValueError, InvalidToken, OSError):
            raise ConnectionInvalid('storage_unavailable') from None

    def _write(self, rows):
        payload = self._cipher().encrypt(json.dumps(rows, ensure_ascii=False).encode())
        temp = self.root / ('.pending-' + uuid.uuid4().hex)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(payload); f.flush(); os.fsync(f.fileno())
            os.replace(temp, self.root / 'connections.enc')
        finally:
            temp.unlink(missing_ok=True)

    @property
    def revision(self):
        path = self.root / 'connections.enc'
        return (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None

    def all(self):
        with self._locked(): return self._read()

    def get(self, identifier):
        row = next((r for r in self.all() if r['id'] == identifier), None)
        if row is None: raise ConnectionInvalid('connection_not_found')
        return row

    def save(self, record, expected_version=None):
        with self._locked():
            rows = self._read()
            old = next((r for r in rows if r['id'] == record['id']), None)
            if expected_version is not None and (old is None or old['version'] != expected_version):
                raise ConnectionInvalid('connection_changed')
            if record.get('sourceId') and any(r.get('sourceId') == record['sourceId'] and r['id'] != record['id'] for r in rows):
                raise ConnectionInvalid('connection_changed')
            if old is None and len(rows) >= 100: raise ConnectionInvalid('connection_limit')
            record = deepcopy(record)
            record['version'] = (old['version'] if old else 0) + 1
            record['updatedAt'] = time.time()
            self._write([r for r in rows if r['id'] != record['id']] + [record])
            return record

    def delete(self, identifier):
        with self._locked():
            rows = self._read()
            self._write([r for r in rows if r['id'] != identifier])


def public_record(record):
    result = {key: deepcopy(record[key]) for key in ('id', 'provider', 'label', 'config', 'updatedAt')} | {
        'secretFields': sorted(k for k, v in record['secrets'].items() if v), 'status': 'connected'}
    if record.get('sourceId'):
        result['sourceId'] = record['sourceId']
        result['fixedConfig'] = deepcopy(record.get('fixedConfig', {}))
    if record.get('credentialOrigin'): result['hasExistingCredential'] = True
    return result


def _origin(value):
    try:
        url = urlsplit(value or '')
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port not in (None, 443):
            return None
        return 'https://' + url.hostname.lower()
    except (ValueError, TypeError):
        return None


class QuotaConnections:
    def __init__(self, root, *, reader=read_connection, clock=time.monotonic):
        self.store = ConnectionStore(root)
        self.reader = reader
        self.clock = clock
        self._locks = {}
        self._logins = {}
        self._targets = {}

    @property
    def revision(self): return self.store.revision

    def catalog(self): return catalog()
    def list(self): return {'connections': [public_record(r) for r in self.store.all()]}

    def register_targets(self, issues):
        """Keep native source metadata private; never serialize inherited keys."""
        targets = {}
        for issue in issues:
            if issue.source_id and provider_spec(issue.setup_provider):
                targets[issue.source_id] = dict(provider=issue.setup_provider,
                    config=deepcopy(issue.setup_config or {}), label=issue.provider,
                    secrets=deepcopy(issue.setup_secrets or {}), origin=_origin(issue.setup_origin))
        self._targets = targets

    def recovery(self, source_id=None, record=None):
        target = self._targets.get(source_id)
        provider = record['provider'] if record else target['provider'] if target else None
        spec = provider_spec(provider)
        if not spec: return None
        result = dict(provider=provider, config=deepcopy(record['config'] if record else target['config']),
                      fixedConfig=deepcopy(record.get('fixedConfig', {}) if record else target['config']),
                      action='login' if spec.get('login') else 'configure' if provider == 'custom' else 'credentials')
        if source_id: result['sourceId'] = source_id
        if record: result['connectionId'] = record['id']
        if (target and target['origin'] and target['secrets'].get('apiKey')) or (record and record.get('credentialOrigin')):
            result['hasExistingCredential'] = True
        return result

    def _draft(self, body, identifier=None, *, login=False):
        if not isinstance(body, dict): raise ConnectionInvalid()
        existing = self.store.get(identifier) if identifier else None
        source_id = body.get('sourceId', existing.get('sourceId') if existing else None)
        if source_id is not None and (not isinstance(source_id, str) or not source_id or len(source_id) > 160):
            raise ConnectionInvalid()
        if existing and existing.get('sourceId') != source_id: raise ConnectionInvalid('connection_changed')
        target = self._targets.get(source_id)
        if source_id and not existing:
            if not target: raise ConnectionInvalid('source_changed')
            existing = next((r for r in self.store.all() if r.get('sourceId') == source_id), None)
            if existing: identifier = existing['id']
        provider = body.get('provider', existing['provider'] if existing else None)
        if existing and existing['provider'] != provider: raise ConnectionInvalid()
        if target and target['provider'] != provider: raise ConnectionInvalid()
        spec = provider_spec(provider)
        if not spec: raise ConnectionInvalid()
        config = deepcopy(body.get('config', existing['config'] if existing else {}))
        fixed_config = deepcopy(existing.get('fixedConfig', {}) if existing else target['config'] if target else {})
        if not isinstance(config, dict) or any(key in config and config[key] != value for key, value in fixed_config.items()):
            raise ConnectionInvalid('connection_changed')
        config = fixed_config | config
        supplied = body.get('secrets', {})
        if not isinstance(supplied, dict): raise ConnectionInvalid()
        secrets = deepcopy(existing['secrets'] if existing else {})
        if set(supplied) - SECRET_FIELDS[provider]: raise ConnectionInvalid()
        for key, value in supplied.items():
            if not isinstance(value, str): raise ConnectionInvalid()
            if value.strip(): secrets[key] = value.strip()
        credential_origin = existing.get('credentialOrigin') if existing else None
        if provider == 'custom':
            if supplied.get('apiKey', '').strip():
                credential_origin = None
            elif credential_origin and config.get('authScheme') != 'none' and _origin(config.get('url')) != credential_origin:
                raise ConnectionInvalid('credential_required')
            elif not secrets.get('apiKey') and target and target['secrets'].get('apiKey') and config.get('authScheme') != 'none':
                if not target['origin'] or _origin(config.get('url')) != target['origin']:
                    raise ConnectionInvalid('credential_required')
                secrets['apiKey'] = target['secrets']['apiKey']
                credential_origin = target['origin']
        cleaned = validate(provider, config, secrets, login=login)
        label = body.get('label', existing['label'] if existing else target.get('label') if target else spec['label']) or spec['label']
        if not isinstance(label, str) or len(label) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise ConnectionInvalid()
        record = dict(id=identifier or str(uuid.uuid4()), provider=provider, label=label.strip(), config=cleaned,
                      secrets=secrets, version=existing['version'] if existing else 0)
        if source_id: record.update(sourceId=source_id, fixedConfig=fixed_config)
        if credential_origin: record['credentialOrigin'] = credential_origin
        return record

    async def _read(self, record):
        result = await asyncio.wait_for(self.reader(record['provider'], record['config'], record['secrets']), 35)
        if isinstance(result, AccountQuota): result = QuotaReadResult((result,))
        if not isinstance(result, QuotaReadResult) or not result.accounts:
            issue = result.issues[0] if isinstance(result, QuotaReadResult) and result.issues else None
            raise QuotaUnavailable(issue.state if issue else 'unavailable', reason=issue.reason if issue else 'quota_not_returned')
        if record['provider'] == 'custom':
            result = replace(result, accounts=tuple(replace(a, account_label=record['label']) for a in result.accounts))
        return result

    def _persist_refresh(self, record, original):
        """Persist a renewal only while the saved credentials still match.

        Another process may edit or disconnect this source during the network
        request. Its newer state wins; a renewal must never resurrect it.
        """
        if record['secrets'] == original:
            return
        try:
            saved = self.store.get(record['id'])
            if saved['version'] == record['version'] and saved['secrets'] == original:
                saved['secrets'] = record['secrets']
                self.store.save(saved, record['version'])
        except ConnectionInvalid:
            # A failed renewal write must not mask the original query error.
            pass

    async def test(self, body, identifier=None):
        lock = self._locks.setdefault(identifier or 'draft', asyncio.Lock())
        async with lock:
            record = self._draft(body, identifier)
            original = deepcopy(record['secrets'])
            try:
                await self._read(record)
                return {'ok': True}
            finally:
                if identifier:
                    self._persist_refresh(record, original)

    async def save(self, body, identifier=None):
        lock = self._locks.setdefault(identifier or 'draft', asyncio.Lock())
        async with lock:
            record = self._draft(body, identifier)
            original = deepcopy(record['secrets'])
            try:
                await self._read(record)
            except BaseException:
                if identifier:
                    self._persist_refresh(record, original)
                raise
            if record.get('sourceId') and not identifier and record['sourceId'] not in self._targets:
                raise ConnectionInvalid('source_changed')
            return {'connection': public_record(self.store.save(record, record['version'] or None))}

    async def delete(self, identifier):
        async with self._locks.setdefault(identifier, asyncio.Lock()):
            self.store.delete(identifier)
        return {'ok': True}

    async def read_accounts(self, *, with_versions=False):
        limiter = asyncio.Semaphore(4)
        async def one(initial):
            async with limiter, self._locks.setdefault(initial['id'], asyncio.Lock()):
                try:
                    row = self.store.get(initial['id'])
                except ConnectionInvalid:
                    return None
                original = deepcopy(row['secrets'])
                try:
                    result = await self._read(row)
                except QuotaUnavailable as error:
                    result = QuotaReadResult(issues=(QuotaIssue(error.state, reason=error.reason),))
                except Exception:
                    result = QuotaReadResult(issues=(QuotaIssue('unavailable', reason='request_failed'),))
                finally:
                    self._persist_refresh(row, original)
                try:
                    current = self.store.get(row['id'])
                except ConnectionInvalid:
                    return None
                # Refresh can advance exactly one revision. Any other change
                # invalidates the result obtained with the old configuration.
                renewed = row['secrets'] != original
                if (current['version'] != row['version'] + int(renewed)
                        or current['config'] != row['config']
                        or current['secrets'] != row['secrets']):
                    return None
                value = (row['id'], row['label'], result)
                return (*value, current['version']) if with_versions else value
        try:
            sources = self.store.all()
        except ConnectionInvalid:
            value = ('connections', '额度连接', QuotaReadResult(issues=(QuotaIssue('unavailable', reason='storage_unavailable'),)))
            return [(*value, None) if with_versions else value]
        tasks = [asyncio.create_task(one(row)) for row in sources]
        try:
            done, pending = await asyncio.wait(tasks, timeout=38) if tasks else (set(), set())
            results = []
            for task, row in zip(tasks, sources):
                value = (row['id'], row['label'], QuotaReadResult(issues=(QuotaIssue('unavailable', reason='request_failed'),)))
                results.append(task.result() if task in done else (*value, row['version']) if with_versions else value)
            return [row for row in results if row is not None]
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending: task.cancel()
            if pending: await asyncio.gather(*pending, return_exceptions=True)

    def begin_login(self, body):
        record = self._draft(body, body.get('connectionId'), login=True)
        if record['provider'] != 'stepfun-plan': raise ConnectionInvalid()
        self._logins = {k: v for k, v in self._logins.items() if self.clock() - v['created'] < 600}
        if len(self._logins) >= 8: raise ConnectionInvalid('login_in_progress')
        identifier = secure.token_urlsafe(32)
        self._logins[identifier] = dict(created=self.clock(), status='waiting', record=record,
                                      existing=record['id'] if record['version'] else None, claimed=False)
        return {'loginId': identifier}

    def _login(self, identifier):
        session = self._logins.get(identifier)
        if not session or self.clock() - session['created'] >= 600:
            self._logins.pop(identifier, None)
            raise ConnectionInvalid('login_expired')
        return session

    def login_state(self, identifier):
        session = self._login(identifier)
        return {key: session[key] for key in ('status', 'connectionId', 'reason') if key in session}

    def claim_login(self, identifier):
        session = self._login(identifier)
        if session['claimed'] or session['status'] != 'waiting': raise ConnectionInvalid('login_in_progress')
        session['claimed'] = True
        return {'provider': session['record']['provider'], 'config': session['record']['config']}

    def cancel_login(self, identifier):
        session = self._login(identifier)
        if session['status'] in ('waiting', 'verifying'):
            session['status'] = 'cancelled'
        return self.login_state(identifier)

    async def complete_login(self, identifier, body):
        session = self._login(identifier)
        if session['status'] != 'waiting': return self.login_state(identifier)
        if not session['claimed']: raise ConnectionInvalid('login_not_started')
        if body.get('cancelled'):
            return self.cancel_login(identifier)
        if body.get('reason'):
            session.update(status='failed', reason='login_failed')
            return self.login_state(identifier)
        session['status'] = 'verifying'
        draft = session['record']
        async with self._locks.setdefault(session['existing'] or 'draft', asyncio.Lock()):
            try:
                if session['existing'] and self.store.get(session['existing'])['version'] != draft['version']:
                    raise ConnectionInvalid('connection_changed')
                record = self._draft(dict(provider=draft['provider'], label=draft['label'], config=draft['config'],
                                          **({'sourceId': draft['sourceId']} if draft.get('sourceId') else {}),
                                          secrets=body.get('secrets', {})), session['existing'])
                if not session['existing'] and record['version']:
                    raise ConnectionInvalid('connection_changed')
                await self._read(record)
                if session['status'] == 'cancelled': return self.login_state(identifier)
                self._login(identifier)
                if record.get('sourceId') and not session['existing'] and record['sourceId'] not in self._targets:
                    raise ConnectionInvalid('source_changed')
                saved = self.store.save(record, record['version'] or None)
            except QuotaUnavailable as error:
                if session['status'] != 'cancelled': session.update(status='failed', reason=error.reason or 'request_failed')
            except ConnectionInvalid as error:
                if session['status'] != 'cancelled': session.update(status='failed', reason=error.reason)
            except Exception:
                if session['status'] != 'cancelled': session.update(status='failed', reason='request_failed')
            else:
                session.update(status='connected', connectionId=saved['id'])
        return self.login_state(identifier)
