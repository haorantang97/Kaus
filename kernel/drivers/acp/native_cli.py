"""Native CLI paths for known adapters; no shell command parsing from clients."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def native_path(driver):
    """Resolve the connection's selected native binary, including custom names.

    A missing explicitly selected installation must not fall back to a different
    binary on PATH. Detection, version management and terminal handoff share this.
    """
    preset = getattr(driver, 'preset', None)
    if preset is None:
        return None
    spec = driver.agent_spec
    env = spec.build_env()
    variable = getattr(preset, 'native_executable_env', None)
    chosen = env.get(variable) if variable else None
    chosen = chosen or getattr(getattr(driver, 'assembly_spec', None), 'options', {}).get('native_cli_path')
    if not chosen and preset.native_cli_flag and preset.native_cli_flag in spec.command:
        index = spec.command.index(preset.native_cli_flag)
        if index + 1 < len(spec.command):
            chosen = spec.command[index + 1]
    if (not chosen and (not preset.native_executable or preset.native_executable == preset.executable)
            and preset.command and preset.command[0] == preset.executable
            and spec.command and Path(spec.command[0]).is_absolute()):
        chosen = spec.command[0]
    if chosen:
        path = Path(chosen).expanduser()
        return str(path.absolute()) if path.is_absolute() else (shutil.which(str(path), path=env.get('PATH')) or str(path.absolute()))
    return shutil.which(preset.native_executable or preset.executable or '', path=env.get('PATH'))

def supports_native_cli(preset):
    return bool(preset and preset.native_cli_selectable and (preset.native_executable_env or preset.native_cli_flag
                           or (preset.executable and preset.command[0] == preset.executable)))


def validated_cli_path(value):
    if not isinstance(value, str) or not value.strip() or "\0" in value or "\n" in value:
        raise ValueError("请选择可执行文件")
    path = Path(value.strip()).expanduser()
    # Preserve the symlink launch name (Node/npm entrypoints may depend on it).
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("可执行文件不存在或没有执行权限")
    return str(path.absolute())


def apply_native_cli(command, process_env, preset, path):
    """Return a launch spec using the selected engine, retaining its ACP adapter."""
    if not path:
        return tuple(command), dict(process_env)
    path = validated_cli_path(path)
    if not supports_native_cli(preset):
        raise ValueError("当前适配器不支持指定原生 CLI 路径")
    env = dict(process_env)
    env["PATH"] = str(Path(path).parent) + os.pathsep + env.get("PATH", os.environ.get("PATH", ""))
    if preset.native_executable_env:
        env[preset.native_executable_env] = path
        return tuple(command), env
    if preset.native_cli_flag:
        flag = preset.native_cli_flag
        argv = list(command)
        if flag in argv:
            index = argv.index(flag)
            if index + 1 >= len(argv):
                raise ValueError("适配器启动参数不完整")
            argv[index + 1] = path
        else:
            argv.extend((flag, path))
        return tuple(argv), env
    return (path, *preset.command[1:]), env
