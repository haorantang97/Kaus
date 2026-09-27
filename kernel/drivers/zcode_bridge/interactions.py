"""Reverse native calls require an active owned turn and explicit client answer."""
from __future__ import annotations

import asyncio

from drivers.stdio_bridge import BridgeError
from .shapes import permission_options, question_schema, validate_answers


class Interactions:
    def __init__(self, session):
        self.session = session
        self.requests = {}

    async def owns(self, ident):
        session = self.session
        if ident in {session.native_id, *session.children}:
            return True
        if not isinstance(ident, str) or not ident:
            return False
        # A child can request permission before its parent tool event reaches us.
        # The native query validates parentID and subagent_child before listing it.
        try:
            result = await session.native.call('session/subagents', {
                'sessionId': session.native_id}, timeout=5)
        except Exception:
            return False
        session.children.update(child for child in result.get('childSessionIds', [])
                                if isinstance(child, str) and child)
        return ident in session.children

    async def request(self, method, params):
        session = self.session
        if method == 'session/requestRuntimePreferences':
            if not session.materializing and params.get('sessionId') not in {session.native_id, *session.children}:
                raise BridgeError('Unknown native session', -32602)
            return {'nativeSearchEnhancementsEnabled': False, 'memoryEnabled': False,
                    'askUserQuestionAutoResolutionEnabled': False,
                    'modelContextBudgetStrategy': 'preflight-v1'}
        if method not in ('interaction/requestPermission', 'interaction/requestUserInput'):
            raise BridgeError('ZCode requires an unsupported desktop callback: ' + method, -32601)
        if (not session.turn or session.turn.done() or not await self.owns(params.get('sessionId'))
                or session.turn.done() or (params.get('turnId') and session.turn_id and params['turnId'] != session.turn_id
                    and params.get('sessionId') == session.native_id)):
            raise BridgeError('Interaction does not belong to an active session turn', -32602)
        key = (params.get('sessionId'), params.get('requestId'))
        if not key[1]:
            raise BridgeError('Native interaction has no request ID', -32602)
        existing = self.requests.get(key)
        if existing:
            if existing[0] != (method, params):
                raise BridgeError('Repeated native interaction changed its content', -32602)
            return await asyncio.shield(existing[1])
        if len(self.requests) >= 256:
            raise BridgeError('Too many native interactions in one turn')
        task = asyncio.create_task(self.answer(method, params))
        self.requests[key] = ((method, params), task)
        return await asyncio.shield(task)

    async def answer(self, method, params):
        permission = method == 'interaction/requestPermission'
        fallback = {'decision': 'deny', 'reason': 'The client cancelled the request'} if permission else {'action': 'cancel'}
        try:
            if permission:
                shown, originals = permission_options(params)
                if not shown:
                    return {'decision': 'deny', 'reason': 'No supported permission options'}
                result = await self.session.bridge.peer.call('session/request_permission', {
                    'sessionId': self.session.ident,
                    'toolCall': {'toolCallId': params.get('toolCallId'), 'status': 'pending',
                                 'title': params.get('reason') or params.get('toolName') or '权限',
                                 'rawInput': params.get('input'),
                                 '_meta': {'nativeSessionId': params.get('sessionId')}},
                    'options': shown})
                outcome = result.get('outcome') or {}
                if outcome.get('outcome') != 'selected':
                    return fallback
                return originals.get(outcome.get('optionId'), fallback)
            schema = question_schema(params)
            result = await self.session.bridge.peer.call('elicitation/create', {
                'sessionId': self.session.ident, 'mode': 'form',
                'message': params.get('prompt') or '需要你的回复', 'requestedSchema': schema})
            action = result.get('action')
            if action != 'accept':
                return {'action': 'decline' if action == 'decline' else 'cancel'}
            return {'action': 'accept', 'content': validate_answers(schema, result.get('content'))}
        except asyncio.CancelledError:
            return fallback
        except Exception:
            # An absent/invalid UI response closes the native wait without granting access.
            return fallback

    async def cancel(self):
        tasks = [entry[1] for entry in self.requests.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def reset(self):
        self.requests.clear()
