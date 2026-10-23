"""Build a managed launch only when a selected-skill policy was materialized."""
from pathlib import Path
import json

from drivers.acp.client import AcpAgentSpec
from drivers.base import UnsupportedCapabilityError
from drivers.hermes.group_execution import hermes_python


def policy_required(home: Path) -> bool:
    marker = home / ".kaus-scope.json"
    return ((home / ".kaus-skills.json").exists()
            or bool(marker.exists() and json.loads(marker.read_text()).get("skillPolicyRequired")))


def command_for(hermes_bin: str, home: Path, args: tuple[str, ...], *, scope=None, native_source=None, native_output=None) -> tuple[str, ...]:
    if not policy_required(home) and native_source is None:
        return (hermes_bin, *args)
    python = hermes_python(AcpAgentSpec(command=(hermes_bin,)))
    if python is None:
        raise UnsupportedCapabilityError("无法找到支持技能隔离的 Hermes 运行环境")
    return (python, "-B", str(Path(__file__).with_name("skill_launcher.py")),
            *(("--kaus-native-source", str(native_source), "--kaus-native-output", str(native_output)) if native_source else ()),
            *(("--kaus-skill-scope", str(scope)) if scope else ()), *args)
