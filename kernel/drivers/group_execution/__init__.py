"""Explicit engine adapters selected only by the installed preset catalog."""
from importlib import import_module


def adapter_for(module_name):
    if not module_name or not module_name.startswith('drivers.'):
        return None
    return import_module(module_name)
