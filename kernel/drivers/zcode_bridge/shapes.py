"""Explicit translations for public native settings, content and interactions."""
from __future__ import annotations

import base64
import json

from drivers.stdio_bridge import BridgeError

MODES = {'build': '询问', 'edit': '自动编辑', 'yolo': '完全访问', 'plan': '计划'}


def model_key(ref: dict) -> str:
    return json.dumps([ref.get('providerId', ''), ref.get('modelId', '')], separators=(',', ':'))


def options(snapshot: dict) -> list[dict]:
    settings = snapshot.get('settings') or {}
    model = settings.get('model') or {}
    available = [row for row in model.get('available', []) if row.get('ref') and not row.get('disabledReason')]
    result = []
    if available:
        result.append({'id': 'model', 'name': '模型', 'category': 'model', 'type': 'select',
            'currentValue': model_key(model.get('current') or {}),
            'options': [{'value': model_key(row['ref']),
                         'name': f"{row.get('providerLabel') or row['ref']['providerId']} · {row.get('label') or row['ref']['modelId']}"}
                        for row in available]})
    thought = settings.get('thoughtLevel') or {}
    if thought.get('enabled') and thought.get('available'):
        result.append({'id': 'thought_level', 'name': '思考', 'category': 'thought_level', 'type': 'select',
            'currentValue': thought.get('current') or thought.get('defaultLevel') or thought['available'][0]['value'],
            'options': [{'value': row['value'], 'name': row.get('label') or row['value']} for row in thought['available']]})
    return result


def mcp_servers(servers: list[dict]) -> list[dict]:
    result, seen = [], set()
    for server in servers:
        name = server.get('name')
        if not isinstance(name, str) or not name or name in seen:
            raise BridgeError('MCP servers require distinct names', -32602)
        seen.add(name)
        kind = server.get('type', 'stdio')
        if kind == 'stdio' and isinstance(server.get('command'), str):
            row = {'name': name, 'command': server['command'], 'args': server.get('args', []), 'env': server.get('env', [])}
        elif kind in ('http', 'sse') and isinstance(server.get('url'), str):
            row = {'name': name, 'type': kind, 'url': server['url'], 'headers': server.get('headers', [])}
        else:
            raise BridgeError('Unsupported MCP transport', -32602)
        result.append({**row, 'isolation': 'session'})
    return result


def prompt_parts(blocks: list[dict]) -> tuple[str, list[dict]]:
    text, attachments = [], []
    for index, block in enumerate(blocks):
        kind = block.get('type')
        if kind == 'text':
            text.append(str(block.get('text', '')))
        elif kind == 'resource_link':
            text.append(f"{block.get('name', '')}: {block.get('uri', '')}")
        elif kind == 'resource' and isinstance(block.get('resource'), dict) and 'text' in block['resource']:
            r = block['resource']
            attachments.append({'kind': 'file', 'filename': r.get('uri', f'file-{index}'), 'textContent': r['text']})
        else:
            if kind == 'image':
                data, mime, name = block.get('data', ''), block.get('mimeType', 'image/png'), f'image-{index}'
            elif kind == 'resource' and isinstance(block.get('resource'), dict):
                r = block['resource']
                data, mime, name = r.get('blob', ''), r.get('mimeType', 'application/octet-stream'), r.get('uri', f'file-{index}')
            else:
                raise BridgeError('Unsupported prompt content', -32602)
            try:
                decoded = base64.b64decode(data, validate=True)
            except (ValueError, TypeError):
                raise BridgeError('Invalid attachment encoding', -32602) from None
            if len(decoded) > 5 * 1024 * 1024:
                raise BridgeError('Attachment exceeds native frame limit', -32602)
            if mime.startswith('image/'):
                attachment_kind = 'image'
            elif mime == 'application/pdf':
                attachment_kind = 'pdf'
            elif mime.startswith('text/'):
                attachment_kind = 'file'
            else:
                raise BridgeError('ZCode bridge cannot send this binary attachment type', -32602)
            attachments.append({'kind': attachment_kind, 'filename': name, 'mimeType': mime,
                                'dataBase64': data, 'sizeBytes': len(decoded)})
    return '\n\n'.join(text), attachments


def permission_options(params: dict) -> tuple[list[dict], dict]:
    shown, originals = [], {}
    for option in params.get('options', []):
        response = option.get('response') or {}
        decision = response.get('decision')
        if decision not in ('allow', 'deny'):
            continue
        ident = option.get('optionId')
        if not isinstance(ident, str) or not ident or ident in originals:
            raise BridgeError('Invalid native permission options', -32602)
        kind = 'reject_once' if decision == 'deny' else ('allow_always' if response.get('permissionUpdates') else 'allow_once')
        shown.append({'optionId': ident, 'kind': kind, 'name': option.get('name') or ident})
        originals[ident] = {key: value for key, value in response.items()
                            if key in ('decision', 'reason', 'modifiedInput', 'permissionUpdates')}
    return shown, originals


def question_schema(params: dict) -> dict:
    if isinstance(params.get('schema'), dict) and params['schema'].get('type') == 'object':
        return params['schema']
    properties = {}
    for index, question in enumerate(params.get('questions') or []):
        choices = question.get('options') or []
        values = {'type': 'string'}
        if choices:
            values.update(enum=[c['value'] for c in choices],
                          enumNames=[c.get('label') or c['value'] for c in choices])
        value = {'type': 'array', 'items': values, 'uniqueItems': True} if question.get('multiSelect') else values
        properties[f'answer_{index}'] = {**value, 'title': question.get('question') or question.get('header', '')}
    if not properties:
        properties['answer'] = {'type': 'string', 'title': params.get('prompt') or '回答'}
    return {'type': 'object', 'properties': properties, 'required': list(properties)}


def validate_answers(schema: dict, content: dict) -> dict:
    if not isinstance(content, dict) or set(content) - set(schema.get('properties', {})):
        raise BridgeError('Invalid question answer fields', -32602)
    if set(schema.get('required', [])) - set(content):
        raise BridgeError('Required question answers are missing', -32602)
    def valid(value, spec):
        kind = spec.get('type', 'string')
        if kind == 'string':
            correct = isinstance(value, str)
        elif kind == 'boolean':
            correct = isinstance(value, bool)
        elif kind in ('integer', 'number'):
            import math
            correct = (type(value) in ((int,) if kind == 'integer' else (int, float)) and math.isfinite(value))
        elif kind == 'array':
            correct = isinstance(value, list) and all(valid(item, spec.get('items', {})) for item in value)
            if correct and spec.get('uniqueItems'):
                correct = len({json.dumps(item, sort_keys=True) for item in value}) == len(value)
        else:
            return False
        return correct and ('enum' not in spec or value in spec['enum'])
    if any(not valid(value, schema['properties'][key]) for key, value in content.items()):
        raise BridgeError('Invalid question answer', -32602)
    return content
