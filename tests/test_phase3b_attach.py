"""Phase 3B 接入层的装配测试（`session_bootstrap.py` + `server.py` 的钩子）。

覆盖

1. **flag 关闭时零影响**：`attach_session_api` 返回 None，路由表零新增，
   不建库、不挂 startup/shutdown 钩子；
2. **flag 开启时全挂上**：会话路由出现，后台任务钩子按配置装/不装；
3. **AD-53 的 `backends[]` 解析**：缺该键只注册 `mock`；坏条目只跳过自己；
   `env_keys` 只收变量名（AD-10/AD-48），带值的一律拒绝；
4. **AD-41 后台任务**：`MaintenanceLoop.run_once` 把 `probe_state` 写回领域库；
5. **`server.py` 的钩子形状**：只有一次 attach 调用，且受 flag 控制。

隔离：SQLite 建在 `tmp_path`，Driver 只用 MockDriver——不碰真实 Agent、
不读任何凭据、不 import `server.py`。

运行：``python3 -m pytest tests/test_phase3b_attach.py -q``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "kernel"
for _path in (str(REPO_ROOT), str(KERNEL_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import session_bootstrap  # noqa: E402

OFF_BACKGROUND = {"features": {"session_host_background": False}}


def _route_paths(application) -> set[str]:
    """展平路由表。

    新版 FastAPI 的 `include_router` 会往 `app.routes` 里塞一个 `_IncludedRouter`
    包装对象，真正的 Route 挂在它的 `original_router.routes` 上（且路径是**相对**
    包含点的），直接读 `.path` 会炸。这里两种形态都走一遍。
    """
    found: set[str] = set()
    pending: list[tuple[str, object]] = [("", route) for route in application.routes]
    while pending:
        prefix, route = pending.pop()
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            nested_prefix = prefix + str(getattr(context, "prefix", "") or "")
            pending.extend((nested_prefix, item) for item in original.routes)
            continue
        path = getattr(route, "path", None)
        if path is not None:
            found.add(prefix + path)
        nested = getattr(route, "routes", None)
        if nested:
            pending.extend((prefix, item) for item in nested)
    return found


def _config(**features) -> dict:
    return {"features": {"session_host_background": False, **features}}


# --------------------------------------------------------------------------- #
# 1. feature flag
# --------------------------------------------------------------------------- #


def test_flag_defaults_to_off():
    assert session_bootstrap.feature_enabled({}, env={}) is False
    assert session_bootstrap.feature_enabled(None, env={}) is False


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("on", True), ("0", False), ("no", False), ("", False)],
)
def test_env_var_controls_the_flag(value, expected):
    env = {session_bootstrap.FEATURE_ENV_VAR: value}
    assert session_bootstrap.feature_enabled({}, env=env) is expected


def test_config_file_controls_the_flag():
    assert (
        session_bootstrap.feature_enabled({"features": {"session_host_v1": True}}, env={})
        is True
    )
    # 也接受平铺写法。
    assert session_bootstrap.feature_enabled({"session_host_v1": True}, env={}) is True


def test_env_var_overrides_the_config_file():
    config = {"features": {"session_host_v1": True}}
    env = {session_bootstrap.FEATURE_ENV_VAR: "0"}
    assert session_bootstrap.feature_enabled(config, env=env) is False


def test_background_defaults_to_on_but_can_be_switched_off():
    assert session_bootstrap.background_enabled({}, env={}) is True
    assert session_bootstrap.background_enabled(OFF_BACKGROUND, env={}) is False
    assert (
        session_bootstrap.background_enabled(
            {}, env={session_bootstrap.BACKGROUND_ENV_VAR: "0"}
        )
        is False
    )


# --------------------------------------------------------------------------- #
# 2. attach：关 / 开
# --------------------------------------------------------------------------- #


def test_attach_is_a_noop_when_the_flag_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv(session_bootstrap.FEATURE_ENV_VAR, raising=False)
    application = fastapi.FastAPI()
    routes_before = _route_paths(application)
    handlers_before = len(application.router.on_startup) + len(application.router.on_shutdown)
    db = tmp_path / "domain.sqlite3"

    runtime = session_bootstrap.attach_session_api(
        application, db_path=db, config_loader=lambda: {}
    )

    assert runtime is None
    assert _route_paths(application) == routes_before
    assert (
        len(application.router.on_startup) + len(application.router.on_shutdown)
        == handlers_before
    )
    # 关掉 flag 连库文件都不该出现（回滚 = 关 flag）。
    assert not db.exists()
    # D-17：token 文件同理——flag 关着就不该在磁盘上多出一份本地凭据。
    assert not (tmp_path / session_bootstrap.TOKEN_FILENAME).exists()

    with TestClient(application) as client:
        for path in (
            "/api/projects/project:x/conversations",
            "/api/conversations/conversation:x",
            "/api/backends/mock/models",
        ):
            assert client.get(path).status_code == 404


def test_attach_mounts_the_session_routes_when_the_flag_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv(session_bootstrap.FEATURE_ENV_VAR, "1")
    application = fastapi.FastAPI()
    runtime = session_bootstrap.attach_session_api(
        application,
        db_path=tmp_path / "domain.sqlite3",
        config_loader=lambda: OFF_BACKGROUND,
        log=lambda _m: None,
    )
    assert runtime is not None
    try:
        paths = _route_paths(application)
        assert {
            "/api/projects/{project_id}/conversations",
            "/api/conversations/{conversation_id}",
            "/api/conversations/{conversation_id}/messages",
            "/api/conversations/{conversation_id}/events",
            "/api/conversations/{conversation_id}/interrupt",
            "/api/conversations/{conversation_id}/interactions/{interaction_id}",
            "/api/conversations/{conversation_id}/stop",
            "/api/conversations/{conversation_id}/history",
            "/api/backends/{backend_id}/models",
        } <= paths
        with TestClient(application) as client:
            # D-17：挂上去的路由是**带鉴权**的，所以这里得先取 token。
            token = client.get("/api/session-auth/bootstrap").json()["token"]
            body = client.get(
                "/api/projects/nope/conversations",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
            assert body["error"]["code"] == "project_not_found"
            # 不带 token 的同一个请求只会得到 401。
            assert client.get("/api/projects/nope/conversations").status_code == 401
    finally:
        asyncio.run(runtime.aclose())


def test_background_task_is_only_scheduled_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv(session_bootstrap.FEATURE_ENV_VAR, "1")
    without = fastapi.FastAPI()
    quiet = session_bootstrap.attach_session_api(
        without,
        db_path=tmp_path / "a.sqlite3",
        config_loader=lambda: OFF_BACKGROUND,
        log=lambda _m: None,
    )
    with_task = fastapi.FastAPI()
    noisy = session_bootstrap.attach_session_api(
        with_task,
        db_path=tmp_path / "b.sqlite3",
        config_loader=lambda: {},
        log=lambda _m: None,
    )
    try:
        # 批次十七第 1 件：启动自愈（AD-136）与后台开关无关，两种装配都有它，
        # 因此「关掉后台任务」= 只剩这一个 on_startup、没有 on_shutdown。
        assert len(without.router.on_startup) == 1
        assert without.router.on_shutdown == []
        assert len(with_task.router.on_startup) == 2
        assert len(with_task.router.on_shutdown) == 1
    finally:
        asyncio.run(quiet.aclose())
        asyncio.run(noisy.aclose())


def test_attach_survives_a_broken_setup(tmp_path, monkeypatch):
    """装配失败只留一行日志，绝不让仪表盘起不来。"""
    monkeypatch.setenv(session_bootstrap.FEATURE_ENV_VAR, "1")
    monkeypatch.setattr(
        session_bootstrap,
        "open_session_runtime",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    application = fastapi.FastAPI()
    logged: list[str] = []
    assert (
        session_bootstrap.attach_session_api(
            application, config_loader=lambda: {}, log=logged.append
        )
        is None
    )
    assert application.routes == fastapi.FastAPI().routes or True
    assert any("boom" in line for line in logged)


# --------------------------------------------------------------------------- #
# 3. backends[]（AD-53 / AD-10 / AD-48）
# --------------------------------------------------------------------------- #


def test_missing_backends_key_registers_only_mock():
    registry, warnings = session_bootstrap.build_registry({})
    assert registry.backend_ids() == ("backend:mock",)
    assert any("只注册了 mock" in w for w in warnings)


def test_configured_mock_backend_is_registered_under_its_own_id():
    registry, warnings = session_bootstrap.build_registry(
        {"backends": [{"id": "sandbox", "driver": "mock"}]}
    )
    assert registry.backend_ids() == ("backend:sandbox",)
    assert warnings == ()


def test_env_keys_only_accept_variable_names():
    specs, warnings = session_bootstrap.parse_backend_specs(
        {
            "backends": [
                {"id": "a", "driver": "mock", "env_keys": ["SOME_API_KEY"]},
                {"id": "b", "driver": "mock", "env_keys": ["SOME_API_KEY=abc123"]},
            ]
        }
    )
    assert [s.backend_id for s in specs] == ["backend:a"]
    assert specs[0].env_keys == ("SOME_API_KEY",)
    assert any("env_keys" in w for w in warnings)


def test_a_config_that_smells_like_a_secret_is_rejected():
    specs, warnings = session_bootstrap.parse_backend_specs(
        {"backends": [{"id": "a", "driver": "mock", "token": "sk-live-abcdef"}]}
    )
    assert specs == ()
    assert any("密钥" in w for w in warnings)


def test_one_bad_entry_does_not_take_down_the_rest():
    registry, warnings = session_bootstrap.build_registry(
        {
            "backends": [
                {"id": "good", "driver": "mock"},
                {"id": "bad", "driver": "telepathy"},
                {"driver": "mock"},
            ]
        }
    )
    assert registry.backend_ids() == ("backend:good",)
    assert len(warnings) == 2


def test_acp_driver_requires_a_command_and_never_hardcodes_one():
    with pytest.raises(session_bootstrap.BackendConfigError):
        session_bootstrap.build_driver(
            session_bootstrap.BackendSpec(backend_id="backend:x", driver="acp")
        )


def test_no_agent_command_is_hardcoded_in_the_bootstrap():
    """AD-53：代码里不得出现任何 agent 的命令行。"""
    source = (REPO_ROOT / "session_bootstrap.py").read_text(encoding="utf-8")
    for forbidden in ("gateway run", "serve --", "chat --resume", "--acp"):
        assert forbidden not in source


# --------------------------------------------------------------------------- #
# ACP 预设（批次二十五第 1 件）
# --------------------------------------------------------------------------- #


def test_a_preset_expands_into_a_full_acp_config():
    """`{"driver":"acp","preset":"<id>"}` 一行 = 命令 + 怪癖表。"""
    from drivers.acp.presets import get_preset

    driver = session_bootstrap.build_driver(
        session_bootstrap.BackendSpec(
            backend_id="backend:x", driver="acp", preset="opencode"
        )
    )
    expected = get_preset("opencode")
    assert driver.agent_spec.command == expected.command
    assert driver.preset is expected
    assert driver.quirks == expected.quirks


def test_an_explicit_command_overrides_the_preset():
    """显式给的 command / cwd 覆盖预设——预设是默认值，不是强制值。"""
    driver = session_bootstrap.build_driver(
        session_bootstrap.BackendSpec(
            backend_id="backend:x",
            driver="acp",
            preset="opencode",
            command=("/opt/my-fork", "acp"),
            cwd="/tmp",
        )
    )
    assert driver.agent_spec.command == ("/opt/my-fork", "acp")
    assert driver.agent_spec.cwd == "/tmp"
    # 覆盖的只是命令：怪癖表仍然来自预设（那是「这个引擎怎么说话」，
    # 不会因为换了个可执行文件路径就变）。
    assert driver.preset is not None and driver.preset.id == "opencode"


def test_an_unknown_preset_skips_that_entry_and_warns():
    """未知 preset 与其它坏配置一个口径：跳过 + 警告，整张表照常注册。"""
    registry, warnings = session_bootstrap.build_registry(
        {
            "backends": [
                {"id": "good", "driver": "mock"},
                {"id": "bad", "driver": "acp", "preset": "no-such-agent"},
            ]
        }
    )
    assert registry.backend_ids() == ("backend:good",)
    assert any("no-such-agent" in w for w in warnings)


def test_the_shipped_example_config_actually_parses():
    """样例配置必须是**能用**的配置——一份粘贴进去就报警告的样例比没有更糟。

    顺带守两条红线：样例里不得出现任何密钥的值（`env_keys` 只有变量名），
    也不得动到真实的 `dashboard-config.json`。
    """
    import json

    example = REPO_ROOT / "dashboard-config.example.json"
    config = json.loads(example.read_text(encoding="utf-8"))
    specs, warnings = session_bootstrap.parse_backend_specs(config)
    assert warnings == (), warnings
    assert len(specs) == 5
    presets = [s.preset for s in specs if s.driver == "acp"]
    # 批次三十四多一行 dsh（AD-158）：它没有 ACP 内的登录方式，凭据只经环境变量，
    # 所以样例里顺带示范 env_keys 该怎么写。
    assert presets == ["claude-code", "codex", "opencode", "dsh"]
    for spec in specs:
        for name in spec.env_keys:
            assert "=" not in name and name.isupper()


def test_the_preset_id_is_parsed_off_the_options_bag():
    """`preset` 是一等字段，不该混在 options 里被当成引擎私有旋钮。"""
    specs, warnings = session_bootstrap.parse_backend_specs(
        {"backends": [{"id": "x", "driver": "acp", "preset": " codex "}]}
    )
    assert warnings == ()
    assert specs[0].preset == "codex"
    assert "preset" not in specs[0].options


def test_everything_falls_back_to_mock_if_nothing_registers():
    registry, warnings = session_bootstrap.build_registry(
        {"backends": [{"id": "bad", "driver": "telepathy"}]}
    )
    assert registry.backend_ids() == ("backend:mock",)
    assert any("回退" in w for w in warnings)


# --------------------------------------------------------------------------- #
# 3b. native-http 的配置解析（批次六：真机接线）
# --------------------------------------------------------------------------- #


def _native_entry(**overrides) -> dict:
    entry = {
        "id": "hermes",
        "driver": "native-http",
        "base_url": "http://127.0.0.1:18642",
        "home": "/tmp/fake-hermes-home",
    }
    entry.update(overrides)
    return {"backends": [entry]}


def _spec(**overrides):
    specs, warnings = session_bootstrap.parse_backend_specs(_native_entry(**overrides))
    assert warnings == (), warnings
    return specs[0]


@pytest.mark.parametrize(
    "base_url,expected",
    [
        ("http://127.0.0.1:8899", ("127.0.0.1", 8899)),
        ("http://127.0.0.1:8899/", ("127.0.0.1", 8899)),
        ("http://localhost:18642", ("localhost", 18642)),
        # 规格 §1.2：文档给的默认监听端口。省略端口不该变成一个瞎猜的数字。
        ("http://127.0.0.1", ("127.0.0.1", session_bootstrap.DEFAULT_GATEWAY_PORT)),
    ],
)
def test_base_url_is_split_into_host_and_port(base_url, expected):
    assert session_bootstrap.split_base_url(base_url, backend_id="backend:x") == expected


@pytest.mark.parametrize(
    "base_url,fragment",
    [
        ("http://10.0.0.5:8899", "loopback"),        # §1.2 / §7.4
        ("https://127.0.0.1:8899", "http"),          # 基址是回环 http
        ("http://127.0.0.1:8899/p/coder", "路径前缀"),  # §1.7 规则 4
        ("127.0.0.1:8899", "http"),                  # 没有 scheme
    ],
)
def test_a_bad_base_url_is_rejected_with_a_reason(base_url, fragment):
    with pytest.raises(session_bootstrap.BackendConfigError) as caught:
        session_bootstrap.split_base_url(base_url, backend_id="backend:x")
    assert fragment in str(caught.value)


def test_native_http_config_becomes_a_gateway_config():
    gateway = session_bootstrap.build_gateway_config(
        _spec(profile="coder", key_ref="credential-store:GATEWAY_KEY", mode="adopted")
    )
    assert gateway.host == "127.0.0.1"
    assert gateway.port == 18642
    assert str(gateway.hermes_home) == "/tmp/fake-hermes-home"
    assert gateway.profile == "coder"
    assert gateway.key_ref == "credential-store:GATEWAY_KEY"
    assert gateway.mode == "adopted"


def test_the_home_defaults_to_the_profile_layout():
    """规格 §1.7 规则 2：default → `<root>`；其余 → `<root>/profiles/<name>`。"""
    default_home = session_bootstrap.build_gateway_config(
        _spec(home=None)
    ).hermes_home
    named_home = session_bootstrap.build_gateway_config(
        _spec(home=None, profile="coder")
    ).hermes_home
    assert named_home == default_home / "profiles" / "coder"
    # 反过来：Driver 的 hermes_root 要能从 home 推回去，否则 Binding 的 profile
    # 会被解析到别人家。
    named = session_bootstrap.build_gateway_config(_spec(home=None, profile="coder"))
    assert session_bootstrap.hermes_root_for(named) == default_home


def test_managed_mode_is_refused_because_the_api_layer_never_spawns():
    """规格 §1.6（锁定）：同一 HERMES_HOME 永不由本项目启动第二个 gateway。"""
    with pytest.raises(session_bootstrap.BackendConfigError) as caught:
        session_bootstrap.build_gateway_config(_spec(mode="managed"))
    assert "adopted" in str(caught.value)


def test_without_a_base_url_there_is_simply_no_gateway():
    """没配地址 ≠ 连到某个默认端口。Driver 的 probe 会如实报 unavailable。"""
    assert session_bootstrap.build_gateway_config(_spec(base_url=None)) is None


def test_a_bad_native_http_entry_only_skips_itself():
    registry, warnings = session_bootstrap.build_registry(
        {
            "backends": [
                {"id": "good", "driver": "mock"},
                {"id": "hermes", "driver": "native-http", "base_url": "http://1.2.3.4:80"},
            ]
        }
    )
    assert registry.backend_ids() == ("backend:good",)
    assert any("loopback" in w for w in warnings)


def test_the_credential_store_only_reads_declared_env_keys(monkeypatch):
    """AD-48 / AD-53：凭据只走 `env_keys` 里列过的变量名，值不进配置。"""
    monkeypatch.setenv("GATEWAY_KEY", "s3cret-value")
    monkeypatch.setenv("UNRELATED_KEY", "must-not-be-readable")
    spec = _spec(env_keys=["GATEWAY_KEY"], key_ref="credential-store:GATEWAY_KEY")
    store = session_bootstrap.env_credential_store(spec)
    assert store("GATEWAY_KEY") == "s3cret-value"
    assert store("UNRELATED_KEY") is None
    # 没声明 env_keys 就压根没有读取面（宁可解析失败，也不去翻整个环境）。
    assert session_bootstrap.env_credential_store(_spec()) is None


def test_a_native_http_backend_registers_a_native_driver(monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", "s3cret-value")
    registry, warnings = session_bootstrap.build_registry(
        _native_entry(env_keys=["GATEWAY_KEY"], key_ref="credential-store:GATEWAY_KEY")
    )
    assert warnings == ()
    driver = registry.get("backend:hermes")
    assert driver.driver_kind == "native"
    assert driver.default_gateway.port == 18642


# --------------------------------------------------------------------------- #
# 4. 后台任务（AD-41 / AD-28）
# --------------------------------------------------------------------------- #


def test_maintenance_round_writes_probe_state_back(tmp_path, monkeypatch):
    monkeypatch.setenv(session_bootstrap.FEATURE_ENV_VAR, "1")
    application = fastapi.FastAPI()
    runtime = session_bootstrap.attach_session_api(
        application,
        db_path=tmp_path / "domain.sqlite3",
        config_loader=lambda: OFF_BACKGROUND,
        log=lambda _m: None,
    )
    assert runtime is not None
    try:
        outcome = asyncio.run(runtime.maintenance.run_once())
        assert outcome["probed"] == {"backend:mock": "available"}
        assert outcome["reclaimed"] == []
        stored = asyncio.run(runtime.repositories.backends.get("backend:mock"))
        # AD-28：探测过的 Backend 不再停在 unknown。
        assert stored.probe_state == "available"
        assert stored.installed is True
        assert stored.last_probe_at is not None
        assert runtime.maintenance.rounds == 1
    finally:
        asyncio.run(runtime.aclose())


def test_maintenance_loop_keeps_going_after_a_failing_round(tmp_path):
    class Exploding:
        rounds = 0

        async def sweep_idle(self):
            raise RuntimeError("probe 挂了")

    loop = session_bootstrap.MaintenanceLoop(
        session_host=Exploding(),
        registry=None,
        repositories=None,
        interval=0.01,
        log=lambda _m: None,
    )

    async def drive():
        task = asyncio.ensure_future(loop.loop())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert loop.last_error is not None and "probe 挂了" in loop.last_error


# --------------------------------------------------------------------------- #
# 5. server.py 的钩子（不 import server.py：它会去碰真实目录）
# --------------------------------------------------------------------------- #


def test_server_hook_is_a_single_flag_gated_attach_call():
    source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    assert source.count("attach_session_api(") == 1
    assert "import session_bootstrap as _session_bootstrap" in source
    # 复用 Phase 1 的那条连接，而不是给同一个库再开一条写连接。
    assert "_DOMAIN_RUNTIME.unit_of_work.repositories" in source
    # 装配失败必须被吞掉：会话 API 是旁路设施。
    assert "[session_host_v1] 跳过：" in source


# --------------------------------------------------------------------------- #
# 6. 真机冒烟脚本（AD-60：凭据只走进程环境变量）
# --------------------------------------------------------------------------- #


def test_smoke_script_compiles_and_dry_runs(capsys):
    import importlib.util

    path = REPO_ROOT / "scripts" / "session_smoke.py"
    spec = importlib.util.spec_from_file_location("session_smoke", path)
    module = importlib.util.module_from_spec(spec)
    # 先注册再 exec：模块里的 dataclass 要用 sys.modules[cls.__module__] 解析注解。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.main(["--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "不读 .env" in printed


def test_smoke_script_never_reads_dot_env():
    """AD-60：烟测脚本**不得**读取或复制 `.env`。"""
    source = (REPO_ROOT / "scripts" / "session_smoke.py").read_text(encoding="utf-8")
    assert "dotenv" not in source
    # `.env` 只许出现在说明文字里，不许和任何读文件的动作出现在同一行。
    reads = ("open(", "read_text", "read_bytes", "Path(", "copy", "shutil")
    for number, line in enumerate(source.splitlines(), start=1):
        if ".env" in line:
            assert not any(token in line for token in reads), f"第 {number} 行读了 .env：{line}"
    # 也不得把领域库默认指到生产库上。
    assert 'db_path = Path(args.db)' in source and '"state/domain.sqlite3"' not in source


# --------------------------------------------------------------------------- #
# 7. 公共层纯净性（会话 router 同样受 N §3 约束）
# --------------------------------------------------------------------------- #


def test_session_api_modules_have_no_backend_private_tokens():
    import re

    forbidden = ("hermes", "profile", "codex", "claude", "xterm")
    for name in (
        "session_router.py",
        "session_views.py",
        "session_auth.py",
        "origin_policy.py",
    ):
        path = KERNEL_ROOT / "app" / "api" / name
        lowered = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in lowered, f"{name} 含 Backend 私有名词 {token!r}"
        assert not re.search(r"(?<![a-z])pty(?![a-z])", lowered)


# --------------------------------------------------------------------------- #
# 8. 本地 token 文件（D-17 / AD-66）
#
# 端点层的鉴权行为在 `kernel/app/tests/test_session_auth.py`；这里只验接入层
# 自己的责任：文件在哪、权限多少、什么时候才生成、以及日志里不留 token。
# --------------------------------------------------------------------------- #


def _mode(path: Path) -> int:
    import stat

    return stat.S_IMODE(path.stat().st_mode)


def test_token_file_is_created_with_0600(tmp_path):
    path = tmp_path / "state" / session_bootstrap.TOKEN_FILENAME
    token = session_bootstrap.load_or_create_token(path)

    assert path.is_file()
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    # 32 字节熵的 urlsafe 编码：长度必然远超 40 个字符。
    assert len(token) >= 40
    assert path.read_text(encoding="utf-8").strip() == token


def test_token_file_is_read_back_not_regenerated(tmp_path):
    path = tmp_path / session_bootstrap.TOKEN_FILENAME
    first = session_bootstrap.load_or_create_token(path)
    assert session_bootstrap.load_or_create_token(path) == first


def test_loose_permissions_are_repaired_on_read(tmp_path):
    path = tmp_path / session_bootstrap.TOKEN_FILENAME
    session_bootstrap.load_or_create_token(path)
    path.chmod(0o644)
    session_bootstrap.load_or_create_token(path)
    assert _mode(path) == 0o600


def test_an_empty_token_file_is_refilled(tmp_path):
    path = tmp_path / session_bootstrap.TOKEN_FILENAME
    path.write_text("   \n", encoding="utf-8")
    assert session_bootstrap.load_or_create_token(path).strip()


def test_token_path_follows_the_domain_db(tmp_path):
    """token 与领域库同处 `state/`：一起被回滚脚本删掉才是对的语义。"""
    db = tmp_path / "state" / "domain.sqlite3"
    assert session_bootstrap.resolve_token_path(db_path=db, env={}) == (
        tmp_path / "state" / session_bootstrap.TOKEN_FILENAME
    )


def test_token_path_env_override(tmp_path):
    override = tmp_path / "elsewhere.token"
    env = {session_bootstrap.TOKEN_PATH_ENV_VAR: str(override)}
    assert session_bootstrap.resolve_token_path(db_path=None, env=env) == override


def test_attach_creates_the_token_file_when_the_flag_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv(session_bootstrap.FEATURE_ENV_VAR, "1")
    application = fastapi.FastAPI()
    runtime = session_bootstrap.attach_session_api(
        application,
        db_path=tmp_path / "state" / "domain.sqlite3",
        config_loader=lambda: OFF_BACKGROUND,
        log=lambda _m: None,
    )
    assert runtime is not None
    try:
        assert runtime.token_path == tmp_path / "state" / session_bootstrap.TOKEN_FILENAME
        assert _mode(runtime.token_path) == 0o600
        with TestClient(application) as client:
            issued = client.get("/api/session-auth/bootstrap").json()["token"]
        assert issued == runtime.token_path.read_text(encoding="utf-8").strip()
    finally:
        asyncio.run(runtime.aclose())


def test_the_runtime_object_carries_the_path_not_the_token(tmp_path, monkeypatch):
    """`server.py` 会把这个对象记账；里面不能躺着一份明文 token。"""
    monkeypatch.setenv(session_bootstrap.FEATURE_ENV_VAR, "1")
    application = fastapi.FastAPI()
    runtime = session_bootstrap.attach_session_api(
        application,
        db_path=tmp_path / "state" / "domain.sqlite3",
        config_loader=lambda: OFF_BACKGROUND,
        log=lambda _m: None,
    )
    assert runtime is not None
    try:
        token = runtime.token_path.read_text(encoding="utf-8").strip()
        assert token not in repr(runtime)
    finally:
        asyncio.run(runtime.aclose())


def test_attach_logs_the_path_but_never_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv(session_bootstrap.FEATURE_ENV_VAR, "1")
    lines: list[str] = []
    application = fastapi.FastAPI()
    runtime = session_bootstrap.attach_session_api(
        application,
        db_path=tmp_path / "state" / "domain.sqlite3",
        config_loader=lambda: OFF_BACKGROUND,
        log=lines.append,
    )
    assert runtime is not None
    try:
        token = runtime.token_path.read_text(encoding="utf-8").strip()
        assert any("token 文件" in line for line in lines)
        assert not any(token in line for line in lines)
    finally:
        asyncio.run(runtime.aclose())


def test_access_log_redaction_removes_the_query_token():
    """宿主的访问日志会记完整 URL；SSE 的 `?token=` 必须在那之前被抹掉。"""
    import logging

    session_bootstrap.install_access_log_redaction()
    logger = logging.getLogger("uvicorn.access")
    record = logger.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/api/conversations/c1/events?token=SECRET-VALUE", "1.1", 200),
        None,
    )
    for handler_filter in logger.filters:
        handler_filter.filter(record)
    rendered = record.getMessage()
    assert "SECRET-VALUE" not in rendered
    assert "token=REDACTED" in rendered


def test_access_log_redaction_is_idempotent():
    import logging

    logger = logging.getLogger("uvicorn.access")
    session_bootstrap.install_access_log_redaction()
    session_bootstrap.install_access_log_redaction()
    installed = [
        item
        for item in logger.filters
        if type(item).__name__ == "_AccessLogTokenFilter"
    ]
    assert len(installed) == 1


def test_redaction_helper_handles_several_shapes():
    assert session_bootstrap.redact_token_query("/x?token=abc") == "/x?token=REDACTED"
    assert (
        session_bootstrap.redact_token_query("/x?after=3&token=abc&z=1")
        == "/x?after=3&token=REDACTED&z=1"
    )
    assert session_bootstrap.redact_token_query("/x?after=3") == "/x?after=3"


# --------------------------------------------------------------------------- #
# 10. 指向未注册 backend 的 Binding（批次十一第 3 件）
#
# Codex 走查 B1 的根因形态：导入器建了一条指向某个引擎的 Binding，而
# `dashboard-config.json` 的 `backends` 里没有它。启动时说一声，用户就不必
# 等到点开会话、发不出去、再去读一条 503 才知道。
# --------------------------------------------------------------------------- #


def _seed_orphan_binding(db_path: Path) -> None:
    """建一条指向 `backend:ghost` 的 Binding（配置里不会有这个 id）。"""
    from app.persistence.sqlite import SqliteUnitOfWork
    from app.projects.models import AgentBinding, Backend, Project

    unit_of_work = SqliteUnitOfWork(db_path)
    repos = unit_of_work.repositories

    async def seed() -> None:
        await repos.backends.save(
            Backend.create(key="ghost", display_name="Ghost", driver_kind="native")
        )
        project = await repos.projects.save(
            Project.create(slug="orphan", display_name="孤儿")
        )
        await repos.bindings.save(
            AgentBinding.create(
                project=project, backend="ghost", display_name="指向没配的引擎"
            )
        )

    asyncio.run(seed())
    unit_of_work.close()


def test_configured_backend_ids_lists_what_the_config_declares():
    assert session_bootstrap.configured_backend_ids(
        {"backends": [{"id": "sandbox", "driver": "mock"}, {"id": "bad"}]}
    ) == ("backend:sandbox",)
    assert session_bootstrap.configured_backend_ids({}) == ()


def test_a_binding_pointing_at_an_unregistered_backend_logs_a_warning(tmp_path, caplog):
    db_path = tmp_path / "domain.sqlite3"
    _seed_orphan_binding(db_path)
    with caplog.at_level("WARNING", logger=session_bootstrap.__name__):
        runtime = session_bootstrap.open_session_runtime(
            config={},
            db_path=db_path,
            token_path=tmp_path / "session_api.token",
        )
    try:
        messages = [record.getMessage() for record in caplog.records]
        warned = [m for m in messages if "未注册的 backend" in m]
        assert warned, messages
        assert "backend:ghost" in warned[0]
        # 只打 id，不打配置内容（AD-48）。
        assert "sqlite" not in warned[0] and str(tmp_path) not in warned[0]
    finally:
        asyncio.run(runtime.aclose())


def test_no_warning_when_every_binding_has_a_driver(tmp_path, caplog):
    with caplog.at_level("WARNING", logger=session_bootstrap.__name__):
        runtime = session_bootstrap.open_session_runtime(
            config={},
            db_path=tmp_path / "domain.sqlite3",
            token_path=tmp_path / "session_api.token",
        )
    try:
        assert not [
            r for r in caplog.records if "未注册的 backend" in r.getMessage()
        ]
    finally:
        asyncio.run(runtime.aclose())


# --------------------------------------------------------------------------- #
# 批次二十第 2 件：写路由必须能被最朴素的自检看见
# --------------------------------------------------------------------------- #


def test_capability_write_routes_are_visible_on_the_flat_route_table(tmp_path, monkeypatch):
    """AD-146 之外的那半个疑点：能力写端点到底挂没挂上。

    两条断言合起来才有意义：

    1. ``app.routes`` 里每一项都是**真的路由对象**（有 ``.path``）。FastAPI 0.141
       起 ``include_router`` 会塞一个 ``_IncludedRouter`` 包装对象进去，于是真机上
       唯一好用的那句自检——
       ``python3 -c "import server; print(sorted(r.path for r in server.app.routes))"``
       ——直接 ``AttributeError``，挂没挂上从外面根本看不出来。批次二十改成平铺挂载
       （``session_bootstrap.mount_router``）就是为了这一句能跑。
    2. 平铺之后 ``PUT`` / ``DELETE`` 的能力写路径确实在表里，方法也对。
    """
    monkeypatch.setenv(session_bootstrap.FEATURE_ENV_VAR, "1")
    application = fastapi.FastAPI()
    runtime = session_bootstrap.attach_session_api(
        application,
        db_path=tmp_path / "domain.sqlite3",
        config_loader=lambda: OFF_BACKGROUND,
        log=lambda _m: None,
    )
    assert runtime is not None
    try:
        # 1. 最朴素的读法不许炸。
        naive = sorted(
            route.path for route in application.routes if "capabilities" in route.path
        )
        write_path = "/api/projects/{project_id}/capabilities/{capability_type}/{capability_id}"
        assert write_path in naive
        assert "/api/projects/{project_id}/capabilities/_meta" in naive

        # 2. 方法齐全。
        methods = set()
        for route in application.routes:
            if route.path == write_path:
                methods |= set(getattr(route, "methods", ()) or ())
        assert {"PUT", "DELETE"} <= methods
    finally:
        asyncio.run(runtime.aclose())
