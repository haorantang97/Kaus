"""Apply a verified conversation snapshot in the installed Hermes process."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys


def main():
    snapshot, digest, target, *args = sys.argv[1:]
    path = Path(snapshot)
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RuntimeError('终端配置校验失败')
    value = json.loads(path.read_text())
    if Path(os.environ.get('HERMES_HOME', '')).resolve() != Path(value['home']):
        raise RuntimeError('终端运行目录与会话不一致')
    from terminal_runtime_guard import install
    launch_args = install(value)
    # --cli is the native explicit interface choice. The Node Ink TUI starts a
    # separate Python gateway and cannot inherit this process-local policy.
    # Insert flags after our Skill launcher's own prefix, before native chat.
    position = args.index('chat')
    args[position:position] = ['--cli', *launch_args]
    try:
        if target == 'native':
            from hermes_cli.main import main as native_main
            sys.argv = ['hermes', *args]
            native_main()
        else:
            sys.argv = [target, *args]
            runpy.run_path(target, run_name='__main__')
    finally:
        from tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
