"""Protocol simulation exercises native semantics, not cloud-model quality."""
import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from drivers.stdio_bridge import BridgeError, Peer
from drivers.zcode_bridge.bridge import Bridge
from drivers.zcode_bridge.shapes import model_key, prompt_parts


def snapshot(ident, cwd):
    return {'protocol': {'name': 'ZCode Protocol', 'version': 1},
        'session': {'sessionId': ident, 'workspace': {'workspacePath': cwd}, 'mode': 'build'},
        'settings': {'model': {'current': {'providerId': 'one', 'modelId': 'same'}, 'available': [
            {'ref': {'providerId': p, 'modelId': 'same'}, 'label': 'same'} for p in ('one', 'two')]},
            'thoughtLevel': {'enabled': True, 'current': 'medium', 'available': [
                {'value': 'low'}, {'value': 'medium'}]}, 'mode': {'current': 'build'}}, 'messages': []}


class FakeNative:
    database = {}
    instances = []

    def __init__(self, command, notify, request, disconnected, *, env, cwd):
        self.notify, self.request, self.disconnected = notify, request, disconnected
        self.env, self.cwd, self.calls, self.closed = env, cwd, [], False
        self.seq, self.input_id, self.turn_id = 0, None, None
        self.instances.append(self)

    async def start(self):
        pass

    async def call(self, method, params, timeout=60):
        self.calls.append((method, deepcopy(params)))
        key = self.env['ZCODE_SESSION_DB_PATH']
        if method == 'runtime/capabilities':
            return {'independentPlanState': True}
        if method == 'session/create':
            self.snapshot = snapshot(str(uuid.uuid4()), self.cwd)
            self.database[key] = self.snapshot
            preferences = await self.request('session/requestRuntimePreferences', {
                'sessionId': self.snapshot['session']['sessionId'], 'scope': 'runtime-materialization'})
            assert preferences['askUserQuestionAutoResolutionEnabled'] is False
        elif method == 'session/resume':
            if key not in self.database or self.database[key]['session']['sessionId'] != params['sessionId']:
                raise BridgeError('Native session missing', -32004)
            self.snapshot = self.database[key]
        elif method == 'session/subscribe':
            return {'sessionId': params['sessionId'], 'eventSeq': self.seq, 'events': []}
        elif method == 'session/subagents':
            return {'childSessionIds': getattr(self, 'children', [])}
        elif method == 'session/setModel':
            self.snapshot['settings']['model']['current'] = params['model']
        elif method == 'session/setThoughtLevel':
            self.snapshot['settings']['thoughtLevel']['current'] = params['thoughtLevel']
        elif method == 'session/setMode':
            self.snapshot['settings']['mode']['current'] = params['mode']
        elif method == 'session/send':
            self.input_id, self.turn_id = params['inputId'], str(uuid.uuid4())
            self.event('turn.started', {'inputId': self.input_id})
            return {'sessionId': params['sessionId'], 'accepted': True}
        elif method == 'session/stop':
            self.event('turn.completed', {'inputId': self.input_id, 'resultType': 'cancelled', 'response': ''})
            return {}
        elif method == 'session/close':
            return {'closed': True}
        else:
            raise AssertionError(method)
        return deepcopy(self.snapshot)

    def event(self, kind, payload, **extra):
        self.seq += 1
        self.notify('session/event', {'sessionId': self.snapshot['session']['sessionId'], 'seq': self.seq,
                    'turnId': self.turn_id, 'type': kind, 'payload': payload, **extra})

    async def close(self):
        self.closed = True


class Contract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.frames = []
        self.peer = Peer(self.frames.append)
        FakeNative.database, FakeNative.instances = {}, []
        self.bridge = Bridge(('fake',), self.root / 'state', self.peer, 2, native_factory=FakeNative)
        await self.bridge.dispatch('initialize', {})

    async def asyncTearDown(self):
        await self.bridge.close()
        self.tmp.cleanup()

    async def create(self, **params):
        result = await self.bridge.dispatch('session/new', {'cwd': str(self.root), 'mcpServers': [], **params})
        return result['sessionId'], self.bridge.sessions[result['sessionId']]

    async def prompt(self, ident, text='test'):
        task = asyncio.create_task(self.bridge.dispatch('session/prompt', {
            'sessionId': ident, 'prompt': [{'type': 'text', 'text': text}]}))
        for _ in range(10):
            await asyncio.sleep(0)
            if self.bridge.sessions[ident].turn_id:
                break
        return task

    async def complete(self, session, task):
        session.native.event('turn.completed', {'inputId': session.input_id,
            'resultType': 'success', 'response': 'answer'})
        return await task

    async def test_separate_normal_planner_reviewer_databases_and_exact_resume(self):
        sessions = [await self.create() for _ in range(3)]
        self.assertEqual(len({s.native_id for _, s in sessions}), 3)
        self.assertEqual(len({s.native.env['ZCODE_SESSION_DB_PATH'] for _, s in sessions}), 3)
        ident, session = sessions[0]
        old_native_id = session.native_id
        await self.bridge.dispatch('session/close', {'sessionId': ident})
        result = await self.bridge.dispatch('session/load', {'sessionId': ident, 'cwd': str(self.root), 'mcpServers': []})
        self.assertEqual(result['sessionId'], ident)
        self.assertEqual(self.bridge.sessions[ident].native_id, old_native_id)
        self.assertNotIn('session/create', [m for m, _ in self.bridge.sessions[ident].native.calls])

    async def test_missing_resume_or_wrong_workspace_never_creates(self):
        with self.assertRaisesRegex(BridgeError, 'original'):
            await self.bridge.dispatch('session/resume', {'sessionId': str(uuid.uuid4()), 'cwd': str(self.root)})
        self.assertEqual(FakeNative.instances, [])
        ident, _ = await self.create()
        await self.bridge.dispatch('session/close', {'sessionId': ident})
        (self.root / 'other').mkdir()
        with self.assertRaisesRegex(BridgeError, 'original'):
            await self.bridge.dispatch('session/resume', {'sessionId': ident, 'cwd': str(self.root / 'other')})
        self.assertEqual(len(FakeNative.instances), 1)

    async def test_ack_does_not_finish_and_stream_deduplicates_terminal_output(self):
        ident, session = await self.create()
        task = await self.prompt(ident)
        self.assertFalse(task.done())
        session.native.event('model.streaming', {'kind': 'reasoning_delta', 'delta': 'native thought'})
        session.native.event('model.streaming', {'kind': 'text_delta', 'delta': 'answer'})
        self.assertEqual((await self.complete(session, task))['stopReason'], 'end_turn')
        updates = [r['params']['update'] for r in self.frames]
        self.assertEqual([r['content']['text'] for r in updates if r['sessionUpdate'] == 'agent_message_chunk'], ['answer'])
        self.assertEqual([r['content']['text'] for r in updates if r['sessionUpdate'] == 'agent_thought_chunk'], ['native thought'])

    async def test_foreign_duplicate_and_stale_events_cannot_finish(self):
        ident, session = await self.create()
        task = await self.prompt(ident)
        payload = {'inputId': session.input_id, 'resultType': 'success', 'response': 'foreign'}
        session.native.event('turn.completed', payload, sessionId='foreign')
        session.native.event('turn.completed', payload, turnId='old-turn')
        session.native.event('turn.completed', {**payload, 'inputId': 'old-input'})
        self.assertFalse(task.done())
        await self.complete(session, task)
        self.assertNotIn('foreign', json.dumps(self.frames))

    async def test_failure_is_not_success_and_retains_safe_cause(self):
        ident, session = await self.create()
        task = await self.prompt(ident)
        session.native.event('turn.failed', {'inputId': session.input_id, 'error': {'message': 'missing API key; token=private123'}})
        with self.assertRaisesRegex(BridgeError, 'missing API key'):
            await task
        self.assertNotIn('private123', str(task.exception()))

    async def test_busy_cancel_and_transport_close(self):
        ident, session = await self.create()
        task = await self.prompt(ident)
        with self.assertRaisesRegex(BridgeError, 'busy'):
            await self.bridge.dispatch('session/prompt', {'sessionId': ident, 'prompt': []})
        await self.bridge.dispatch('session/cancel', {'sessionId': ident})
        self.assertEqual((await task)['stopReason'], 'cancelled')
        task = await self.prompt(ident)
        session.disconnected(BridgeError('native crashed'))
        with self.assertRaisesRegex(BridgeError, 'crashed'):
            await task

    async def test_real_model_options_preserve_provider_and_reasoning(self):
        ident, session = await self.create()
        selected = model_key({'providerId': 'two', 'modelId': 'same'})
        await self.bridge.dispatch('session/set_config_option', {'sessionId': ident, 'configId': 'model', 'value': selected})
        await self.bridge.dispatch('session/set_config_option', {'sessionId': ident, 'configId': 'thought_level', 'value': 'low'})
        calls = session.native.calls[-2:]
        self.assertEqual(calls[0][1]['model']['providerId'], 'two')
        self.assertIs(calls[0][1]['persistAsWorkspaceLastUsed'], False)
        self.assertEqual(calls[1][1]['thoughtLevel'], 'low')
        with self.assertRaises(BridgeError):
            await session.configure('thought_level', 'invented')
        with self.assertRaises(BridgeError):
            await session.mode('auto')

    async def test_mcp_injected_with_session_isolation(self):
        _, session = await self.create(mcpServers=[{'name': 'tool', 'command': '/a/b', 'args': ['--x'], 'env': []}])
        create = next(p for m, p in session.native.calls if m == 'session/create')
        self.assertEqual(create['mcpServers'][0]['isolation'], 'session')
        self.assertNotIn('type', create['mcpServers'][0])

    async def interaction(self, session, method, params, result):
        before = len(self.frames)
        task = asyncio.create_task(session.interactions.request(method, params))
        for _ in range(10):
            await asyncio.sleep(0)
            if len(self.frames) > before and 'id' in self.frames[-1]:
                break
        self.peer.response({'id': self.frames[-1]['id'], 'result': result})
        return await task

    async def test_permission_exact_choice_and_unknown_choice_denied(self):
        ident, session = await self.create()
        prompt = await self.prompt(ident)
        params = {'sessionId': session.native_id, 'requestId': 'r1', 'toolCallId': 't1', 'toolName': 'write',
                  'options': [{'optionId': 'once', 'name': 'Allow once', 'response': {'decision': 'allow', 'modifiedInput': {'safe': True}}}]}
        result = await self.interaction(session, 'interaction/requestPermission', params,
            {'outcome': {'outcome': 'selected', 'optionId': 'once'}})
        self.assertEqual(result, params['options'][0]['response'])
        result = await self.interaction(session, 'interaction/requestPermission', {**params, 'requestId': 'r2'},
            {'outcome': {'outcome': 'selected', 'optionId': 'invented'}})
        self.assertEqual(result['decision'], 'deny')
        await self.complete(session, prompt)

    async def test_question_repeats_share_one_ui_and_multiselect_answers(self):
        ident, session = await self.create()
        prompt = await self.prompt(ident)
        params = {'sessionId': session.native_id, 'requestId': 'q1', 'questions': [
            {'question': 'Choose', 'multiSelect': True, 'options': [{'value': 'a', 'label': 'A'}, {'value': 'b', 'label': 'B'}]}]}
        first = asyncio.create_task(session.interactions.request('interaction/requestUserInput', params))
        second = asyncio.create_task(session.interactions.request('interaction/requestUserInput', params))
        for _ in range(10):
            await asyncio.sleep(0)
            if self.frames:
                break
        self.assertEqual(len(self.frames), 1)
        self.peer.response({'id': self.frames[0]['id'], 'result': {'action': 'accept', 'content': {'answer_0': ['a', 'b']}}})
        self.assertEqual(await first, await second)
        self.assertEqual((await first)['content'], {'answer_0': ['a', 'b']})
        await self.complete(session, prompt)

    async def test_invalid_question_and_foreign_permission_are_closed(self):
        ident, session = await self.create()
        prompt = await self.prompt(ident)
        params = {'sessionId': session.native_id, 'requestId': 'q1', 'questions': [
            {'question': 'Choose', 'options': [{'value': 'a', 'label': 'A'}]}]}
        result = await self.interaction(session, 'interaction/requestUserInput', params,
            {'action': 'accept', 'content': {'answer_0': 'invented'}})
        self.assertEqual(result['action'], 'cancel')
        with self.assertRaisesRegex(BridgeError, 'active'):
            await session.interactions.request('interaction/requestPermission', {**params, 'sessionId': 'foreign'})
        await self.complete(session, prompt)

    async def test_native_owned_child_can_request_before_parent_tool_event(self):
        ident, session = await self.create()
        prompt = await self.prompt(ident)
        session.native.children = ['owned-child']
        params = {'sessionId': 'owned-child', 'requestId': 'child-permission',
            'toolCallId': 'write-file', 'toolName': 'write', 'options': [
                {'optionId': 'deny', 'name': 'Deny', 'response': {'decision': 'deny'}}]}
        result = await self.interaction(session, 'interaction/requestPermission', params,
            {'outcome': {'outcome': 'selected', 'optionId': 'deny'}})
        self.assertEqual(result, {'decision': 'deny'})
        self.assertIn('owned-child', session.children)
        self.assertEqual(session.native.calls[-1], ('session/subagents', {'sessionId': session.native_id}))
        await self.complete(session, prompt)

    async def test_files_images_and_child_attribution(self):
        ident, session = await self.create()
        prompt = await self.prompt(ident)
        session.native.event('part.upserted', {'part': {'partId': 'f', 'type': 'file', 'url': '/tmp/output.pdf', 'mime': 'application/pdf'}})
        session.native.event('tool.updated', {'kind': 'scheduled', 'toolCallId': 'child-tool', 'toolName': 'worker', 'childSessionId': 'child', 'source': 'subagent'})
        data = base64.b64encode(b'png-content').decode()
        session.native.event('tool.updated', {'kind': 'result', 'toolCallId': 'child-tool', 'result': {
            'output': 'done', 'display': {'kind': 'node_repl_images', 'images': [{'base64': data, 'mimeType': 'image/png'}]}}})
        session.native.event('part.upserted', {'part': {'type': 'tool', 'callId': 'child-tool',
            'tool': 'worker', 'state': {'status': 'completed', 'output': 'done'}}})
        latest = [r['params']['update'] for r in self.frames
            if r['params']['update'].get('toolCallId') == 'child-tool'][-1]
        self.assertIn(data, json.dumps(latest))
        await self.complete(session, prompt)
        self.assertIn('child', session.children)
        self.assertIn('file:///tmp/output.pdf', json.dumps(self.frames))
        self.assertIn(data, json.dumps(self.frames))

    def test_embedded_prompt_content_and_unsupported_binary(self):
        text, attachments = prompt_parts([{'type': 'text', 'text': 'read'}, {'type': 'resource', 'resource': {
            'uri': 'note.md', 'text': 'context'}}, {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(b'image').decode()}])
        self.assertEqual(text, 'read')
        self.assertEqual(attachments[0]['textContent'], 'context')
        self.assertEqual(attachments[1]['kind'], 'image')
        with self.assertRaises(BridgeError):
            prompt_parts([{'type': 'resource', 'resource': {'blob': 'YQ==', 'mimeType': 'application/octet-stream'}}])
