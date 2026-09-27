#!/usr/bin/env python3
"""Exercise the real OpenClaw ACP bridge with an isolated, model-free Gateway.

This does not read the user's configuration, inherit model credentials, install a
service, send a model prompt, or approve a permission. All native state is kept in
a fresh directory beneath the requested output directory. Use an already
downloaded OpenClaw package and a compatible Node binary.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kernel"))
from drivers.acp.client import AcpAgentSpec, AcpConnection, AcpRpcError  # noqa: E402


def isolated_environment(root: Path, node: Path, token: str) -> dict[str, str]:
    """Provide process essentials, isolation roots, and only our fresh test token."""
    return {
        "PATH": f"{node.parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(root / "tmp"),
        "LANG": "en_US.UTF-8",
        # Official product root: absolute paths bypass OS-home fallback, and
        # its presence disables native service identity and home session scans.
        "OPENCLAW_HOME": str(root / "openclaw-home"),
        "OPENCLAW_STATE_DIR": str(root / "state"),
        "OPENCLAW_CONFIG_PATH": str(root / "openclaw.json"),
        "OPENCLAW_GATEWAY_TOKEN": token,
        "OPENCLAW_LOAD_SHELL_ENV": "0",
        "OPENCLAW_EXEC_SHELL_SNAPSHOT": "0",
        "OPENCLAW_OFFLINE": "1",
        "OPENCLAW_NO_AUTO_UPDATE": "1",
        "OPENCLAW_HIDE_BANNER": "1",
        "OPENCLAW_SUPPRESS_NOTES": "1",
    }


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def probe_kaus_default(root: Path, spec: AcpAgentSpec, timeout: float) -> dict:
    """Run the real driver through Group APIs in a fresh SQLite test harness."""
    from app.conversations.models import Conversation
    from app.projects.models import AgentBinding, Backend
    from app.tests.test_batch22_groups import GroupHarness, _api, _new_group
    from drivers.acp.driver import AcpDriver
    from drivers.acp.presets import get_preset

    domain_root = root / "kaus-state"
    domain_root.mkdir()
    built = GroupHarness(domain_root, coordinator_enabled=True)
    await built.seed()
    driver = AcpDriver(spec, backend_key="openclaw", preset=get_preset("openclaw"), call_timeout=timeout)
    built.registry.register(driver)
    report = {}
    try:
        await built.repositories.backends.save(Backend.create(key="openclaw", display_name="OpenClaw", driver_kind="acp"))
        binding = await built.repositories.bindings.save(AgentBinding.create(
            project=built.project_id, backend="openclaw", discriminator="isolated-probe",
            runtime_config={"workspace_root": str(root / "workspace")},
        ))
        driver.register_binding(binding)
        catalog = await driver.get_model_catalog(binding)
        report["catalog"] = catalog.model_dump(mode="json", by_alias=True)
        report["controls"] = driver.conversation_controls(binding)
        assert catalog.engine_default_available and not catalog.degraded and not catalog.models
        normal = await built.repositories.conversations.save(Conversation.create(
            project_id=built.project_id, agent_binding_id=binding.id, title="Isolated normal contract",
        ))
        normal_handle = await built.host.ensure_runtime(normal)
        async with _api(built) as api:
            group = await _new_group(api, "Isolated native coordinator contract")
            response = await api.put(f"/api/groups/{group['id']}/coordinator", json={
                "config": {"bindingId": binding.id, "modelId": None, "reasoningMode": "low"},
                "expectedRevision": 0,
            })
            report["configStatus"] = response.status_code
            report["configResponse"] = response.json()
            assert response.status_code == 200
            group = await built.repositories.collaborations.get(group["id"])
            state = {"configSnapshot": group.settings["coordinator"]}
            handles = [normal_handle]
            for role in ("planning", "review"):
                conversation = await built.group_router.group_coordinator.execution(group, state, role)
                handles.append(await built.host.ensure_runtime(conversation))
            report["nativeSessionIds"] = [h.native_session_id for h in handles]
            report["distinctNativeSessions"] = len(set(report["nativeSessionIds"])) == 3
            assert report["distinctNativeSessions"]
            for handle in handles:
                await built.host.stop_runtime(handle.conversation_id)
            normal = await built.repositories.conversations.get(normal.id)
            resumed = await built.host.ensure_runtime(normal)
            report["reconnectedSameNativeSession"] = resumed.native_session_id == normal_handle.native_session_id
            report["runtimeResumed"] = resumed.metadata.get("resumed")
            assert report["reconnectedSameNativeSession"] and report["runtimeResumed"]
        report["ok"] = True
    finally:
        await built.aclose()
    return report


async def run_probe(args: argparse.Namespace) -> dict:
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="openclaw-contract-", dir=output.parent))
    for name in ("tmp", "state", "workspace", "openclaw-home"):
        (root / name).mkdir()
    node, cli = Path(args.node).resolve(), Path(args.cli).resolve()
    port, token = available_port(), secrets.token_urlsafe(32)
    env = isolated_environment(root, node, token)
    token_file = root / "test-gateway.token"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)
    config = {
        "gateway": {
            "mode": "local", "bind": "loopback", "port": port,
            "auth": {"mode": "token"},
            "controlUi": {"enabled": False},
        },
        "agents": {"defaults": {"workspace": str(root / "workspace")}},
        "plugins": {"enabled": False},
        "discovery": {"mdns": {"mode": "off"}},
        "logging": {"file": str(root / "gateway.log")},
        "update": {"checkOnStart": False},
    }
    (root / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    report: dict = {
        "kind": "kaus.openclaw.native-acp-contract.v1",
        "startedAt": time.time(), "isolatedRoot": str(root),
        "node": str(node), "cli": str(cli),
        "modelPromptSent": False, "permissionApprovals": 0,
        "checks": [], "notifications": [],
    }
    logs: list[str] = []
    connections: list[AcpConnection] = []
    gateway = None
    log_tasks: list[asyncio.Task] = []

    async def drain(stream):
        while line := await stream.readline():
            logs.append(line.decode(errors="replace").rstrip().replace(token, "[TEST_TOKEN]"))
            if len(logs) > 120:
                del logs[0]

    async def connect() -> AcpConnection:
        connection = await AcpConnection.spawn(AcpAgentSpec(
            command=(str(node), str(cli), "acp", "--url", f"ws://127.0.0.1:{port}", "--token-file", str(token_file)),
            cwd=str(root / "workspace"), env=env, inherit_env=False,
        ))
        connections.append(connection)
        connection.on_notification = lambda method, params: report["notifications"].append(
            {"method": method, "params": params}
        )

        async def reject_request(rpc_id, method, params):
            if method == "session/request_permission":
                connection.respond(rpc_id, result={"outcome": {"outcome": "cancelled"}})
            else:
                connection.respond(rpc_id, error={"code": -32601, "message": "Probe has no client tools"})

        connection.on_request = reject_request
        result = await connection.call("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "kaus-isolated-contract", "version": "1"},
        }, timeout=args.timeout)
        report["checks"].append({"name": "initialize", "ok": True, "result": result})
        return connection

    async def call(connection, name, method, params):
        try:
            result = await connection.call(method, params, timeout=args.timeout)
        except AcpRpcError as exc:
            result = {"code": exc.code, "message": exc.message, "data": exc.data}
            report["checks"].append({"name": name, "ok": False, "error": result})
            return None
        report["checks"].append({"name": name, "ok": True, "result": result})
        return result

    try:
        gateway = await asyncio.create_subprocess_exec(
            str(node), str(cli), "gateway", "run", "--port", str(port), "--bind", "loopback",
            "--auth", "token", "--allow-unconfigured", "--ws-log", "compact",
            cwd=str(root / "workspace"), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        log_tasks = [asyncio.create_task(drain(gateway.stdout)), asyncio.create_task(drain(gateway.stderr))]
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if gateway.returncode is not None:
                raise RuntimeError(f"Isolated Gateway exited with code {gateway.returncode}")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.2)
        else:
            raise TimeoutError("Isolated Gateway did not open its loopback port")
        report["checks"].append({"name": "isolated_gateway", "ok": True})
        connection = await connect()
        session_params = {"cwd": str(root / "workspace"), "mcpServers": []}
        first = await call(connection, "new_first", "session/new", session_params)
        second = await call(connection, "new_second", "session/new", session_params)
        if not first or not second:
            raise RuntimeError("Could not create both isolated ACP sessions")
        sid, sid2 = first["sessionId"], second["sessionId"]
        report["checks"].append({"name": "distinct_session_ids", "ok": sid != sid2})
        await call(connection, "set_thought_low", "session/set_config_option", {
            "sessionId": sid, "configId": "thought_level", "value": "low",
        })
        await call(connection, "set_mode_medium", "session/set_mode", {"sessionId": sid, "modeId": "medium"})
        listing = await call(connection, "list_after_patch", "session/list", {})
        await call(connection, "model_config_rejected", "session/set_config_option", {
            "sessionId": sid, "configId": "model", "value": "probe-unused-model",
        })
        await call(connection, "per_session_mcp_rejected", "session/new", {
            **session_params, "mcpServers": [{"name": "unlaunched-test", "command": "/usr/bin/false", "args": [], "env": []}],
        })
        await call(connection, "close_second", "session/close", {"sessionId": sid2})
        await connection.aclose()
        resumed = await connect()
        resume_params = {**session_params, "sessionId": sid}
        await call(resumed, "resume_new_process", "session/resume", resume_params)
        await call(resumed, "load_new_process", "session/load", resume_params)
        await call(resumed, "close_loaded", "session/close", {"sessionId": sid})
        canonical = next((s["sessionId"] for s in (listing or {}).get("sessions", [])
                          if s.get("sessionId", "").endswith(sid)), None)
        if canonical:
            await call(resumed, "resume_canonical_gateway_key", "session/resume", {
                **session_params, "sessionId": canonical,
            })
            await call(resumed, "close_canonical", "session/close", {"sessionId": canonical})
        if args.kaus:
            report["kaus"] = await probe_kaus_default(root, AcpAgentSpec(
                command=(str(node), str(cli), "acp", "--url", f"ws://127.0.0.1:{port}", "--token-file", str(token_file)),
                cwd=str(root / "workspace"), env=env, inherit_env=False,
            ), args.timeout)
        report["completed"] = True
    except Exception as exc:
        report["completed"] = False
        report["failure"] = {"type": type(exc).__name__, "message": str(exc).replace(token, "[TEST_TOKEN]")}
    finally:
        for connection in connections:
            with contextlib.suppress(Exception):
                await connection.aclose()
        if gateway is not None and gateway.returncode is None:
            gateway.terminate()
            try:
                await asyncio.wait_for(gateway.wait(), timeout=10)
            except asyncio.TimeoutError:
                gateway.kill()
                await gateway.wait()
        if log_tasks:
            await asyncio.gather(*log_tasks, return_exceptions=True)
        token_file.unlink(missing_ok=True)
        report["gatewayLogTail"] = logs[-30:]
        report["finishedAt"] = time.time()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2).replace(token, "[TEST_TOKEN]") + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--kaus", action="store_true", help="Also verify normal and Group runtime creation through Kaus")
    args = parser.parse_args()
    report = asyncio.run(run_probe(args))
    print(json.dumps({"completed": report["completed"], "out": str(Path(args.out).resolve()),
                      "checks": [{"name": c["name"], "ok": c["ok"]} for c in report["checks"]]}, ensure_ascii=False))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
