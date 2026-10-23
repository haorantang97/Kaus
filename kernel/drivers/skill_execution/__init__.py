"""Native loading policies selected by the preset, shared by all conversation roles."""
from importlib import import_module
import re


def adapter_for(module_name):
    if not module_name or not (module_name.startswith("drivers.skill_execution.") or
                               re.fullmatch(r"drivers\.[a-z_]+\.skill_adapter", module_name)):
        return None
    return import_module(module_name)
