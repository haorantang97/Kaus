"""批次二十五第 5 件：``GET /api/backends`` 带 ``preset`` 与 ``quirks``（只读）。

覆盖三件事
----------
1. 登记了取数面时，单个 Backend 与列表两条路都带上这两个键，且内容对得上
   Driver 手上的那份预设；
2. **没**登记取数面（会话 flag 关着、或这条 Backend 不是按预设接的）时这两个键
   **整个不出现**——不是 ``null``、不是空对象。前端按「有没有这个键」分支，
   缺字段静默不渲染（AD-71）；
3. 上 wire 的只有名字与布尔位：启动 argv 一个字都不上，``envKeysHint`` 只有
   变量**名**（AD-10 / AD-48）。

隔离：SQLite 建在 ``tmp_path``；不拉起任何进程（只造 Driver 对象，不调
``probe``），一个真实引擎都不碰。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.backend_facets import (  # noqa: E402
    clear_facet_provider,
    facets_from_driver,
    set_facet_provider,
)
from app.api.router import build_domain_router  # noqa: E402
from app.persistence.sqlite import SqliteUnitOfWork  # noqa: E402
from app.projects.models import Backend  # noqa: E402
from drivers.acp.client import AcpAgentSpec  # noqa: E402
from drivers.acp.driver import AcpDriver  # noqa: E402
from drivers.acp.presets import get_preset  # noqa: E402
from drivers.mock.driver import MockDriver  # noqa: E402
from drivers.registry import BackendDriverRegistry  # noqa: E402

PRESET_ID = "codex"


@pytest.fixture
def client(tmp_path: Path):
    unit = SqliteUnitOfWork(tmp_path / "domain.sqlite3")
    repositories = unit.repositories

    async def _seed() -> None:
        for key, kind in (("acp-agent", "acp"), ("mock", "mock")):
            await repositories.backends.save(
                Backend.create(key=key, driver_kind=kind, installed=True)
            )

    asyncio.run(_seed())
    app = fastapi.FastAPI()
    app.include_router(build_domain_router(repositories))
    with TestClient(app) as test_client:
        yield test_client
    unit.close()


@pytest.fixture
def registry():
    preset = get_preset(PRESET_ID)
    # 只造对象，**不** spawn：AcpAgentSpec 里的命令这一整份测试里一次都不执行。
    driver = AcpDriver(
        AcpAgentSpec(command=preset.command), backend_key="acp-agent", preset=preset
    )
    registry = BackendDriverRegistry([driver, MockDriver()])
    yield registry
    clear_facet_provider()


@pytest.fixture
def wired(registry):
    set_facet_provider(
        lambda backend_id: (
            facets_from_driver(registry.try_get(backend_id))
            if registry.try_get(backend_id) is not None
            else None
        )
    )
    return registry


def test_without_a_provider_the_keys_are_simply_absent(client) -> None:
    """AD-71：取数面没登记 = 缺字段，不是 null。"""
    payload = client.get("/api/backends/acp-agent").json()
    assert "preset" not in payload
    assert "quirks" not in payload


def test_a_preset_backed_backend_reports_its_preset_and_quirks(client, wired) -> None:
    payload = client.get("/api/backends/acp-agent").json()
    preset = get_preset(PRESET_ID)
    assert payload["preset"]["id"] == PRESET_ID
    assert payload["preset"]["label"] == preset.label
    assert payload["preset"]["authModel"] == "own-auth"
    assert payload["preset"]["supportsExternalCli"] is bool(
        preset.resume_argv_template
    )
    assert payload["quirks"] == preset.quirks.to_wire()


def test_a_driver_without_a_preset_reports_neither(client, wired) -> None:
    """裸 mock（没有预设）不该长出一个空的预设块。"""
    payload = client.get("/api/backends/mock").json()
    assert "preset" not in payload
    assert "quirks" not in payload


def test_the_list_endpoint_carries_the_same_fields(client, wired) -> None:
    rows = {row["id"]: row for row in client.get("/api/backends").json()["backends"]}
    assert rows["backend:acp-agent"]["preset"]["id"] == PRESET_ID
    assert "preset" not in rows["backend:mock"]


def test_no_launch_argv_and_no_credential_values_reach_the_wire(client, wired) -> None:
    """启动命令不上 wire；``envKeysHint`` 只有变量名（AD-10 / AD-48 / v1.0 §16.6）。"""
    payload = client.get("/api/backends/acp-agent").json()
    blob = repr(payload)
    for part in get_preset(PRESET_ID).command:
        assert part not in blob, "启动 argv 不该出现在能力协商的响应里"
    for name in payload["preset"].get("envKeysHint", ()):
        assert "=" not in name and name.isupper()


#: 怪癖表上 wire 的**布尔**那七位（AD-151）。
QUIRK_BOOLEANS = {
    "toolUpdateCumulative",
    "supportsSessionResume",
    "supportsSessionLoad",
    "supportsSetMode",
    "supportsSetModel",
    "thoughtChunks",
    "needsClientFs",
}

#: AD-158 新增的四位。它们不是布尔，因为它们回答的不是「支不支持」而是「是哪一
#: 种」——压成布尔就得再造一个「另一种是什么」的隐含约定，那正是自由文本的开始。
QUIRK_ENUMS = {
    "modeSemantics": {"approval", "thought_level", "none"},
    "modelSwitch": {"set_model", "config_option", "none"},
    "modelIdFormat": {"plain", "effort_suffix"},
}


def test_quirks_are_a_closed_set_of_bits_not_free_text(client, wired) -> None:
    """怪癖表上 wire 的形状是**闭集**——前端按位渲染，不解析自由文本。

    七位布尔（AD-151）+ 三个闭集枚举 + 一位布尔（``resumeRequiresClose``，AD-158）
    + 两位布尔（``mcpViaSessionNew``、``preferSessionLoad``）。多一个键就会红：这张表是给
    代码读的开关，不是备忘录。
    """
    extra_booleans = {"resumeRequiresClose", "mcpViaSessionNew", "preferSessionLoad"}
    quirks = client.get("/api/backends/acp-agent").json()["quirks"]
    assert set(quirks) == (QUIRK_BOOLEANS | set(QUIRK_ENUMS) | extra_booleans)
    for name in QUIRK_BOOLEANS | extra_booleans:
        assert isinstance(quirks[name], bool), name
    for name, allowed in QUIRK_ENUMS.items():
        assert quirks[name] in allowed, (name, quirks[name])


# ---------------------------------------------------------------- 批次三十二


def test_auth_method_ids_reach_the_wire_as_ids_only(client, wired) -> None:
    """AD-156：``preset.authMethodIds`` 上 wire，且**只有 id**。

    这一列是给「先在终端登录：…」那句话用的。id 之外的东西（登录方式的中文名、
    说明、``vars`` 里的变量名）一个都不该跟着上来——那是一份登录向导，不是
    仪表盘该复述的东西。
    """
    payload = client.get("/api/backends/acp-agent").json()
    ids = payload["preset"]["authMethodIds"]
    assert ids == list(get_preset(PRESET_ID).auth_method_ids)
    assert ids, "codex 这一行取证到过 authMethods，不该是空的"
    blob = repr(payload)
    for noise in ("Login with ChatGPT", "Requires setting", "subscription"):
        assert noise not in blob


def test_a_preset_without_auth_method_ids_omits_the_key(client, wired) -> None:
    """没取证到（或适配器明说没有）时这个键**整个不出现**，不是空列表（AD-71）。"""
    from drivers.acp.presets import PRESETS

    empty = [p for p in PRESETS.values() if not p.auth_method_ids]
    assert empty, "至少有一行还没取证到 authMethods，否则这条断言就失去了对象"
    for preset in empty:
        assert "authMethodIds" not in preset.to_wire()


# ---------------------------------------------------------------- 批次三十四


def test_verified_bits_reach_the_wire_as_bit_names_only(client, wired) -> None:
    """AD-158：``preset.verifiedBits`` 上 wire，取值是怪癖表那七个键名。

    前端要区分「这一格是真机验过的」与「这一格还是照文档填的」，就得有一份能对
    得上的名单；名单里的名字必须与 ``quirks`` 那几个键**逐字同名**，否则两处对不
    起来，界面只能猜。
    """
    payload = client.get("/api/backends/acp-agent").json()
    verified = payload["preset"]["verifiedBits"]
    assert verified == sorted(verified), "排过序才好比对"
    assert set(verified) <= set(payload["quirks"]), "名字必须与 quirks 的键同源"
    assert set(verified) <= QUIRK_BOOLEANS, "取证等级只针对那七位布尔"
    assert verified, "codex 这一行是真机全测过的，不该是空的"


def test_a_preset_without_verified_bits_omits_the_key() -> None:
    """一位都没取证到时这个键**整个不出现**，不是空列表（AD-71）。

    空列表会被读成「测过、一位都不支持」；键不存在读成「还没测过」。两句话在界面
    上导向完全不同的动作，所以不能压成同一种形状。
    """
    from drivers.acp.presets import PRESETS

    empty = [p for p in PRESETS.values() if not p.verified_bits]
    assert empty, "至少有一行还没有真机结论，否则这条断言就失去了对象"
    for preset in empty:
        assert "verifiedBits" not in preset.to_wire()
