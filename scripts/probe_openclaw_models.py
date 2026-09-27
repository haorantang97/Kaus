#!/usr/bin/env python3
"""Native, credential-free OpenClaw Gateway model-selection acceptance.

Starts a fresh isolated Gateway with two fictitious loopback providers. No model
prompt, cloud request, user configuration write, or tool approval is performed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kernel"))
from probe_openclaw import available_port, isolated_environment
from drivers.acp.client import AcpAgentSpec
from drivers.openclaw.driver import OpenClawAcpDriver
from drivers.acp.presets import get_preset
from app.conversations.models import Conversation
from app.projects.models import AgentBinding, Backend
from app.tests.test_batch22_groups import GroupHarness, _api, _new_group


async def run(args):
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="openclaw-models-", dir=output.parent))
    for child in ("tmp", "state", "workspace", "openclaw-home", "kaus-state"):
        (root / child).mkdir()
    node, cli = Path(args.node).resolve(), Path(args.cli).resolve()
    port, token = available_port(), secrets.token_urlsafe(32)
    env = isolated_environment(root, node, token)
    config = {
        "gateway": {"mode": "local", "bind": "loopback", "port": port, "auth": {"mode": "token"}, "controlUi": {"enabled": False}},
        "agents": {"defaults": {"workspace": str(root / "workspace"), "model": {"primary": "kaus_a/shared-model"},
                    "modelSelectionScope": "global", "models": {"kaus_a/shared-model": {}, "kaus_b/shared-model": {}}}},
        "models": {"providers": {provider: {"baseUrl": "http://127.0.0.1:1/v1", "api": "openai-completions", "apiKey": "isolated-test-no-cloud-credential",
                   "models": [{"id": "shared-model", "name": "Shared model", "reasoning": True, "input": ["text"], "contextWindow": 32768, "maxTokens": 1024}]}
                   for provider in ("kaus_a", "kaus_b")}},
        "plugins": {"enabled": False}, "discovery": {"mdns": {"mode": "off"}}, "logging": {"file": str(root / "gateway.log")}, "update": {"checkOnStart": False},
    }
    config_path = root / "openclaw.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)
    initial_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    token_file = root / "test-gateway.token"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)
    report = {"kind": "kaus.openclaw.session-model-contract.v1", "root": str(root), "modelPrompts": 0, "cloudCredentials": False, "initialConfigHash": initial_hash, "checks": {}}
    gateway = None
    logs = []
    built = None

    async def drain(stream):
        while line := await stream.readline():
            logs.append(line.decode(errors="replace").replace(token, "[TEST_TOKEN]").rstrip())
            if len(logs) > 30:
                del logs[0]

    tasks = []
    native_spawn = asyncio.create_subprocess_exec

    async def traced_spawn(*argv, **kwargs):
        process = await native_spawn(*argv, **kwargs)
        if "call" in argv and "gateway" in argv:
            communicate = process.communicate
            async def capture():
                stdout, stderr = await communicate()
                method = argv[argv.index("call") + 1]
                if process.returncode or method == "sessions.patch":
                    report.setdefault("nativeRpcEvidence", []).append({"method": method, "exitCode": process.returncode,
                        "stdout": stdout.decode(errors="replace").replace(token, "[TEST_TOKEN]"),
                        "stderr": stderr.decode(errors="replace").replace(token, "[TEST_TOKEN]")})
                return stdout, stderr
            process.communicate = capture
        return process
    asyncio.create_subprocess_exec = traced_spawn
    try:
        gateway = await asyncio.create_subprocess_exec(str(node), str(cli), "gateway", "run", "--port", str(port), "--bind", "loopback", "--auth", "token", "--allow-unconfigured", "--ws-log", "compact",
            cwd=str(root / "workspace"), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        tasks = [asyncio.create_task(drain(gateway.stdout)), asyncio.create_task(drain(gateway.stderr))]
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if gateway.returncode is not None:
                raise RuntimeError(f"Isolated Gateway exited {gateway.returncode}")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(.2)
        else:
            raise TimeoutError("Gateway startup")
        spec = AcpAgentSpec(command=(str(node), str(cli), "acp", "--url", f"ws://127.0.0.1:{port}", "--token-file", str(token_file)), cwd=str(root / "workspace"), env=env, inherit_env=False)
        driver = OpenClawAcpDriver(spec, backend_key="openclaw", preset=get_preset("openclaw"), call_timeout=args.timeout)
        built = GroupHarness(root / "kaus-state", coordinator_enabled=True)
        await built.seed()
        built.registry.register(driver)
        await built.repositories.backends.save(Backend.create(key="openclaw", display_name="OpenClaw", driver_kind="acp"))
        binding = await built.repositories.bindings.save(AgentBinding.create(project=built.project_id, backend="openclaw", discriminator="isolated-model-probe", runtime_config={"workspace_root": str(root / "workspace")}))
        driver.register_binding(binding)
        catalog = await driver.get_model_catalog(binding)
        report["nativeVersion"] = driver._backend_version
        report["catalog"] = catalog.model_dump(mode="json", by_alias=True)
        assert {m.model_id for m in catalog.models} == {"kaus_a/shared-model", "kaus_b/shared-model"}, catalog
        assert not catalog.degraded
        assert (await driver.read_auth_state(binding)).state == "unknown"
        report["checks"]["provider_qualified_catalog"] = True
        normal = await built.repositories.conversations.save(Conversation.create(project_id=built.project_id, agent_binding_id=binding.id, title="Isolated model contract", model_id="kaus_a/shared-model"))
        handle = await built.host.ensure_runtime(normal)
        state = driver._state(handle)
        first_key = await driver.gateway.session_key(handle.native_session_id, require_existing=True)
        selection_started = time.monotonic()
        await driver.set_conversation_model(handle, "kaus_b/shared-model")
        report["selectionSeconds"] = round(time.monotonic() - selection_started, 2)
        selected = await driver.gateway.call("sessions.describe", {"key": first_key})
        report["selectedSession"] = {k: v for k, v in selected["session"].items() if k in {"key", "sessionId", "modelProvider", "model", "modelOverride", "providerOverride"}}
        assert report["selectedSession"].get("modelProvider") == "kaus_b", selected
        assert driver._snapshot_from_session(state.session_result).current_model_id == "kaus_b/shared-model"
        report["checks"]["session_selection_confirmed"] = True
        await built.repositories.conversations.save((await built.repositories.conversations.get(normal.id)).evolve(model_id="kaus_b/shared-model"))
        await built.host.stop_runtime(normal.id)
        resumed = await built.host.ensure_runtime(await built.repositories.conversations.get(normal.id))
        assert resumed.native_session_id == handle.native_session_id
        assert driver._snapshot_from_session(driver._state(resumed).session_result).current_model_id == "kaus_b/shared-model"
        report["checks"]["reconnect_same_session_selection"] = True
        async with _api(built) as api:
            group = await _new_group(api, "Isolated OpenClaw model Group")
            response = await api.put(f"/api/groups/{group['id']}/coordinator", json={"config": {"bindingId": binding.id, "modelId": "kaus_a/shared-model", "providerId": "kaus_a", "reasoningMode": "low"}, "expectedRevision": 0})
            assert response.status_code == 200, response.json()
            group = await built.repositories.collaborations.get(group["id"])
            handles = [resumed]
            keys = [first_key]
            for role in ("planning", "review"):
                conversation = await built.group_router.group_coordinator.execution(group, {"configSnapshot": group.settings["coordinator"]}, role)
                member_handle = await built.host.ensure_runtime(conversation)
                handles.append(member_handle)
                keys.append(await driver.gateway.session_key(member_handle.native_session_id, require_existing=True))
            assert len(set(keys)) == 3
            for key in keys[1:]:
                row = (await driver.gateway.call("sessions.describe", {"key": key}))["session"]
                assert row["modelProvider"] == "kaus_a"
            report["checks"]["group_roles_have_distinct_native_sessions"] = True
            report["checks"]["group_and_normal_model_isolation"] = True
            report["nativeSessionIds"] = [h.native_session_id for h in handles]
        await asyncio.sleep(.5)
        report["finalConfigHash"] = hashlib.sha256(config_path.read_bytes()).hexdigest()
        assert report["finalConfigHash"] == initial_hash
        report["checks"]["sticky_default_config_unchanged"] = True
        report["completed"] = True
    except Exception as exc:
        report["completed"] = False
        report["failure"] = {"type": type(exc).__name__, "message": str(exc).replace(token, "[TEST_TOKEN]"), "reason": getattr(exc, "reason", None)}
    finally:
        if built is not None:
            await built.aclose()
        if gateway is not None and gateway.returncode is None:
            gateway.terminate()
            try:
                await asyncio.wait_for(gateway.wait(), 10)
            except asyncio.TimeoutError:
                gateway.kill()
                await gateway.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.create_subprocess_exec = native_spawn
        token_file.unlink(missing_ok=True)
        if not report.get("completed"):
            report["gatewayLogTail"] = logs
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=float, default=40)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps({"completed": result.get("completed"), "checks": result.get("checks"), "failure": result.get("failure")}, ensure_ascii=False))
    raise SystemExit(0 if result.get("completed") else 1)
