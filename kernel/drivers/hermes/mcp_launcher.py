"""Compose native Hermes startup with a process-local MCP registration check."""
import runpy
import sys
import os


def main():
    # Install immediately before ACP starts, after the CLI has selected its
    # profile and after any managed Skill/Group launcher has configured itself.
    from acp_adapter import entry
    original = entry.main

    def checked_entry(argv=None):
        if "KAUS_HERMES_MCP_NAMES" in os.environ or os.environ.get("HERMES_ACP_SKIP_CONFIGURED_MCP") == "1":
            from mcp_runtime_guard import install
            install()
        if any(os.environ.get(key) in {"0", "1"} for key in
               ("KAUS_COMPUTER_USE_DESKTOP", "KAUS_COMPUTER_USE_BROWSER")):
            from computer_use_runtime import install as install_computer_use
            install_computer_use()
        if os.environ.get("KAUS_HERMES_HTTP_TRANSPORT") == "1" or os.environ.get("KAUS_HERMES_HTTP_SESSION"):
            from http_session_bridge import install as install_http
            install_http(os.environ.get("KAUS_HERMES_HTTP_SESSION"))
        return original(argv)

    entry.main = checked_entry
    target, args = sys.argv[1], sys.argv[2:]
    if target == 'native':
        from hermes_cli.main import main as native_main
        sys.argv = ['hermes', *args]
        native_main()
    else:
        sys.argv = [target, *args]
        runpy.run_path(target, run_name='__main__')


if __name__ == '__main__':
    main()
