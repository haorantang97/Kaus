"""Read-only readiness report for the exact installed Hermes interpreter."""
import json
import os
import subprocess
import sys

try:
    from .computer_use_runtime import disabled_by_config
except ImportError:
    from computer_use_runtime import disabled_by_config


def browser_ready():
    return browser_readiness()["status"] == "ready"


def browser_readiness():
    from tools.browser_tool import check_browser_routed_requirements
    if check_browser_routed_requirements():
        return {"status": "ready", "installed": True, "exposed": True,
                "selected_tool_enabled": True, "requirements": []}
    # Browser Use replaces browser_* with browser_exec. The former gate then
    # intentionally returns False; do not inject a duplicate fallback service.
    from tools.browser_use_cli import is_browser_use_cli_mode, _find_cli
    command = _find_cli() if is_browser_use_cli_mode() else None
    if command:
        # Native Hermes also returns `uvx browser-use` when only the installer
        # exists. That advertises a tool, but is not an installed browser CLI.
        installed = len(command) == 1
        return {"status": "ready" if installed else "needs_setup", "installed": installed,
                "exposed": True, "selected_tool_enabled": True,
                "requirements": [] if installed else ["browser_cli_install_required"]}
    return {"status": "unavailable", "installed": False, "exposed": False,
            "selected_tool_enabled": False, "requirements": ["browser_dependencies_missing"]}


def desktop_readiness_status():
    """Read macOS TCC without running the unrelated, expensive full doctor."""
    if sys.platform != "darwin":
        from tools.computer_use.permissions import computer_use_status
        return computer_use_status()
    try:
        from tools.computer_use.permissions import _resolve_driver_cmd, _json_out
    except ImportError:
        # Earlier Hermes releases can lack the permissions module helpers.
        # Keep native binary resolution and credential sanitization, then use
        # the installed driver's same read-only status command directly.
        from tools.computer_use.cua_backend import resolve_cua_driver_cmd, cua_driver_child_env
        binary = resolve_cua_driver_cmd(None)
        if not binary:
            return {"installed": False, "ready": None}
        reply = subprocess.run([binary, "permissions", "status", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, env=cua_driver_child_env(), stdin=subprocess.DEVNULL)
        data = json.loads(reply.stdout) if reply.returncode == 0 else None
    else:
        binary = _resolve_driver_cmd(None)
        if not binary:
            return {"installed": False, "ready": None}
        data = _json_out(binary, "permissions", "status", "--json", timeout=5)
    result = {"installed": True, "ready": None}
    if isinstance(data, dict):
        for key in ("accessibility", "screen_recording"):
            if isinstance(data.get(key), bool):
                result[key] = data[key]
    if all(key in result for key in ("accessibility", "screen_recording")):
        result["ready"] = result["accessibility"] and result["screen_recording"]
    return result


def probe():
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    result = {}
    for kind, toolset in (("browser", "browser"), ("desktop", "computer_use")):
        row = {"provider": "hermes-native", "status": "unknown", "requirements": [],
               "installed": False, "selected_tool_enabled": False}
        result[kind] = row
        try:
            if disabled_by_config(config, toolset):
                row.update(status="disabled", requirements=["native_tool_disabled"])
                continue
            if kind == "browser":
                row.update(browser_readiness())
            else:
                from tools.computer_use.tool import check_computer_use_requirements
                ready = check_computer_use_requirements()
                row["installed"] = ready
                if not ready:
                    row.update(status="unavailable", requirements=["cua_driver_missing"])
                    continue
                status = desktop_readiness_status()
                if status.get("installed") is False:
                    row.update(status="unavailable", installed=False, requirements=["cua_driver_missing"])
                    continue
                # TCC belongs to this executable. An unconfigured CuaDriver
                # does not revoke an independently authorized desktop provider.
                # Explicit native/tool policy exclusions are handled above.
                row["status"] = "ready" if status.get("ready") is True else "needs_setup" if status.get("ready") is False else "unknown"
                row["requirements"] = ([] if row["status"] == "ready" else
                    ["computer_system_permission_required"] if row["status"] == "needs_setup" else
                    ["computer_permission_status_unavailable"])
                missing = [name for name, key in (("screen-recording", "screen_recording"),
                           ("accessibility", "accessibility")) if status.get(key) is False]
                if missing:
                    row["permissionSetup"] = {"host": "cua-driver", "permissions": missing}
                row["selected_tool_enabled"] = row["status"] == "ready" and os.environ.get("KAUS_COMPUTER_USE_DESKTOP") == "1"
        except (ImportError, AttributeError):
            row.update(status="unknown" if kind == "browser" else "unavailable",
                       exposed=kind == "browser", requirements=["native_tool_unavailable"])
        except Exception:
            row.update(status="unknown", exposed=kind == "browser",
                       requirements=["native_probe_incomplete"])
    return result


if __name__ == "__main__":
    print(json.dumps(probe()))
