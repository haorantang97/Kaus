"""Codex process-local Group overrides; credentials and saved config stay intact."""
import json
import os


def supported(spec):
    return True


def prepare(spec, role):
    overrides = json.loads(spec.env.get('CODEX_CONFIG') or os.environ.get('CODEX_CONFIG') or '{}')
    overrides = {**overrides, 'memories': {**overrides.get('memories', {}),
                 'use_memories': False, 'generate_memories': False},
                 'features': {**overrides.get('features', {}), 'memory_tool': False}}
    if role == 'review':
        overrides.update(project_doc_max_bytes=0, developer_instructions='')
    return spec.with_env(CODEX_CONFIG=json.dumps(overrides))
