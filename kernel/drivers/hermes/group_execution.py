"""Installed Hermes ACP runtime, with per-process isolated agent construction."""
import os
import shutil
from pathlib import Path
from drivers.acp.client import AcpAgentSpec

def hermes_python(spec: AcpAgentSpec) -> str | None:
    configured = spec.env.get('HERMES_ACP_PYTHON') or os.environ.get('HERMES_ACP_PYTHON')
    executable = shutil.which(spec.command[0]) if spec.command else None
    candidates = [Path(configured)] if configured else []
    if executable:
        candidates.append(Path(executable).resolve().parent / 'python')
    candidates.append(Path.home() / '.hermes/hermes-agent/venv/bin/python')
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)



def supported(spec):
    return hermes_python(spec) is not None


def prepare(spec, role):
    python = hermes_python(spec)
    if not python:
        raise ValueError('无法找到支持独立上下文的 Hermes 运行环境')
    launcher = Path(__file__).with_name('group_launcher.py')
    return spec.model_copy(update={'command': (python, str(launcher), role),
        'env': {**dict(spec.env), 'HERMES_ACP_SKIP_CONFIGURED_MCP': '1'}})
