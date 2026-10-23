"""Lossless CAS references inside an adapter-declared serialized JSON field."""
from __future__ import annotations

import json
import re

ASSET_MARKER = '__kaus_archive_asset_v1__'
JSON_MARKER = '__kaus_archive_json_string_v1__'
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_ESCAPE = re.compile(r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')
_KEY_TAIL = re.compile(r'\s*:')


def pack(original, parsed, extracted):
    replacements = {}

    def compare(before, after):
        if isinstance(before, str) and isinstance(after, dict) and ASSET_MARKER in after:
            replacements[before] = after
        elif isinstance(before, dict) and isinstance(after, dict):
            for key in before:
                compare(before[key], after[key])
        elif isinstance(before, list) and isinstance(after, list):
            for a, b in zip(before, after):
                compare(a, b)

    compare(parsed, extracted)
    if not replacements:
        return original
    parts = []
    end = 0
    for token in _STRING.finditer(original):
        if _KEY_TAIL.match(original, token.end()):
            continue
        decoded = json.loads(token[0])
        replacement = replacements.get(decoded)
        if replacement is None:
            continue
        parts.append(original[end:token.start()])
        # Retain JSON escapes without keeping another copy of the base64 body.
        escapes = []
        raw = token[0][1:-1]
        has_escapes = '\\' in raw
        cursor = 0
        position = 0
        while has_escapes and cursor < len(raw):
            if raw[cursor] == '\\':
                escape = _ESCAPE.match(raw, cursor)
                if escape is None:
                    raise ValueError('archive_record_corrupt')
                escapes.append([position, escape[0]])
                cursor = escape.end()
            else:
                cursor += 1
            position += 1
        parts.append({'value': replacement, 'escapes': escapes})
        end = token.end()
    parts.append(original[end:])
    return {JSON_MARKER: parts}


def _parts(value):
    if set(value) != {JSON_MARKER} or not isinstance(value[JSON_MARKER], list):
        raise ValueError('archive_backup_invalid')
    for part in value[JSON_MARKER]:
        if isinstance(part, str):
            yield part
            continue
        if not isinstance(part, dict) or set(part) != {'value', 'escapes'}:
            raise ValueError('archive_backup_invalid')
        marker = part['value']
        if not isinstance(marker, dict) or set(marker) != {ASSET_MARKER, 'prefix'}:
            raise ValueError('archive_backup_invalid')
        if not isinstance(part['escapes'], list):
            raise ValueError('archive_backup_invalid')
        positions = set()
        for entry in part['escapes']:
            if not isinstance(entry, list) or len(entry) != 2 or type(entry[0]) is not int or entry[0] < 0 or entry[0] in positions or not isinstance(entry[1], str) or not _ESCAPE.fullmatch(entry[1]):
                raise ValueError('archive_backup_invalid')
            positions.add(entry[0])
        yield part


def materialize(value):
    """Expose parsed multimodal blocks without expanding attachment bytes."""
    if isinstance(value, dict) and JSON_MARKER in value:
        return json.loads(''.join(part if isinstance(part, str) else json.dumps(part['value']) for part in _parts(value)))
    if isinstance(value, dict):
        return {key: materialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [materialize(item) for item in value]
    return value


def restore(value, expand):
    output = []
    for part in _parts(value):
        if isinstance(part, str):
            output.append(part)
            continue
        text = expand(part['value'])
        escapes = dict(part['escapes'])
        if any(position >= len(text) or json.loads('"'+escape+'"') != text[position] for position, escape in escapes.items()):
            raise ValueError('archive_object_corrupt')
        encoded = ['"']
        cursor = 0
        for position, escape in sorted(escapes.items()):
            encoded.extend((json.dumps(text[cursor:position])[1:-1], escape))
            cursor = position + 1
        encoded.extend((json.dumps(text[cursor:])[1:-1], '"'))
        output.append(''.join(encoded))
    return ''.join(output)
