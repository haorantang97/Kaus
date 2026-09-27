"""批次三十三（AD-157）：ACP 的登录态与「按需探一次」的模型目录。

真机现象（测试员的 Mac，2026-09-06）：引擎卡写 ``Model: NOT SET``、
``GET /api/backends/{id}/models?binding=`` 回 ``models: []``、输入区没有模型
选择器，**第一句话失败得毫无痕迹**（页面只说「历史没有到达」，侧栏 ``Failed``，
一个字的原因都没有）。三条根因，本文件守前两条在 Driver 侧的落法：

1. 目录此前**只在一次成功的 session/new 之后**才有内容——「还没聊过」被渲染成
   了「这台引擎没有模型」；
2. ``session/new`` 回 ``-32000 Authentication required`` 时没有任何一层把它翻成
   「你还没登录，去终端跑那一句」。

隔离：全部跑在假 agent 子进程上（``--dress`` 装扮成「要登录」/「模型在
configOptions 里」两类真实形状）。不读任何凭据、不碰任何真实引擎家目录；
「登录」这件事用 ``tmp_path`` 下一个标记文件表达。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from drivers.acp.client import AcpRpcError
from drivers.acp.driver import CATALOG_TTL_SECONDS, AcpDriver
from drivers.acp.failure_hints import AUTH_REQUIRED
from drivers.acp.presets import AcpPreset, AgentQuirks
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import AuthRequiredError, CreateSessionOptions

#: 一份**只为本文件存在**的预设：目录里那八行都带产品名，而这个包的纯净性断言
#: 不许测试写出任何一个预设 id（AD-151）。这里要的只是「预设能带一条登录命令」
#: 这个形状，与具体是哪一家无关。
FAKE_PRESET = AcpPreset(
    id="fake-preset",
    label="Fake Engine",
    command=("python3",),
    login_command="fake-cli login",
    quirks=AgentQuirks(supports_set_mode=True),
)


@pytest.fixture
def harness() -> FakeAcpHarness:
    return FakeAcpHarness()


def make_driver(dress: dict | None = None, *, preset: AcpPreset | None = None) -> AcpDriver:
    workdir = tempfile.gettempdir()
    return AcpDriver(
        fake_agent_spec("text-stream", cwd=workdir, dress=dress),
        backend_key="acp",
        default_cwd=workdir,
        call_timeout=20.0,
        prompt_timeout=30.0,
        preset=preset,
    )


def _binding(harness: FakeAcpHarness, driver: AcpDriver):
    return harness.make_binding(harness.make_project(), driver)


# --------------------------------------------------------------------------- #
# 1. 按需探测：还没聊过也要有目录
# --------------------------------------------------------------------------- #


async def test_catalog_is_probed_on_demand_from_available_models(
    harness: FakeAcpHarness,
) -> None:
    """一次会话都没跑过，目录也该是满的（真机 ``Model: NOT SET`` 的根因）。"""
    driver = make_driver()
    binding = _binding(harness, driver)
    catalog = await driver.get_model_catalog(binding)
    assert [m.model_id for m in catalog.models] == ["fake:small", "fake:large"]
    # ``models.currentModelId`` 当作默认值：Binding 自己没写时才用它。
    assert catalog.default_model_id == "fake:small"
    assert catalog.degraded is False and catalog.diagnostics == ()


async def test_catalog_is_probed_from_config_options(harness: FakeAcpHarness) -> None:
    """第二种来源：模型只在 ``configOptions[category=model]`` 里（批次三十二取证）。"""
    driver = make_driver({"configOptions": True})
    binding = _binding(harness, driver)
    catalog = await driver.get_model_catalog(binding)
    assert [m.model_id for m in catalog.models] == ["fake:small", "fake:large"]
    assert [m.display_name for m in catalog.models] == ["Fake Small", "Fake Large"]
    assert catalog.default_model_id == "fake:small"
    # 同一份 configOptions 里的模式与思考档也被收下来了（模式给 set_mode 用）。
    snapshot = driver._catalog[binding.id]  # noqa: SLF001 - 缓存形状是本批的被测面
    assert snapshot.mode_ids == ("default", "bypassPermissions")
    assert snapshot.thought_levels == ("low", "medium")


async def test_probe_does_not_leave_a_session_behind(harness: FakeAcpHarness) -> None:
    """探测开出来的会话不进账本——那不是用户的会话（探完就收）。"""
    driver = make_driver()
    binding = _binding(harness, driver)
    await driver.get_model_catalog(binding)
    assert driver.session_ledger.get(binding.id, {}) == {}
    # 真会话仍然照常记账，两条路互不影响。
    await driver.create_native_session(binding, CreateSessionOptions())
    assert len(driver.session_ledger[binding.id]) == 1


# --------------------------------------------------------------------------- #
# 2. 没登录：空目录 + degraded + 一句修法，而不是异常
# --------------------------------------------------------------------------- #


def _auth_dress(flag: Path, *, methods: tuple[str, ...] = ()) -> dict:
    dress: dict = {"requireAuth": str(flag)}
    if methods:
        dress["authMethods"] = list(methods)
    return dress


async def test_auth_required_gives_a_degraded_catalog_with_a_login_diagnostic(
    harness: FakeAcpHarness, tmp_path: Path
) -> None:
    driver = make_driver(_auth_dress(tmp_path / "logged-in"), preset=FAKE_PRESET)
    binding = _binding(harness, driver)
    catalog = await driver.get_model_catalog(binding)
    assert catalog.models == ()
    assert catalog.degraded is True
    assert catalog.diagnostics == ("需要先在终端登录：fake-cli login",)


async def test_auth_required_shows_up_as_signed_out_with_a_hint(
    harness: FakeAcpHarness, tmp_path: Path
) -> None:
    """引擎卡的登录行：``signed_out`` + 「在终端运行 …」。"""
    driver = make_driver(_auth_dress(tmp_path / "logged-in"), preset=FAKE_PRESET)
    binding = _binding(harness, driver)
    auth = await driver.read_auth_state(binding)
    assert auth.state == "signed_out"
    assert auth.model == "own-auth"
    assert auth.hint == "在终端运行 `fake-cli login`，然后重试"
    assert auth.checked_at is not None


async def test_a_successful_probe_reports_signed_in(harness: FakeAcpHarness) -> None:
    driver = make_driver()
    binding = _binding(harness, driver)
    auth = await driver.read_auth_state(binding)
    assert auth.state == "signed_in" and auth.hint is None


async def test_signed_in_after_the_user_logs_in_in_the_terminal(
    harness: FakeAcpHarness, tmp_path: Path
) -> None:
    """登录发生在站外：标记文件出现之后，**新一次**探测就该是已登录。"""
    flag = tmp_path / "logged-in"
    driver = make_driver(_auth_dress(flag), preset=FAKE_PRESET)
    binding = _binding(harness, driver)
    assert (await driver.read_auth_state(binding)).state == "signed_out"
    flag.write_text("ok", encoding="utf-8")
    driver._catalog.clear()  # noqa: SLF001 - 模拟 TTL 到期
    catalog = await driver.get_model_catalog(binding)
    assert (await driver.read_auth_state(binding)).state == "signed_in"
    assert [m.model_id for m in catalog.models] == ["fake:small", "fake:large"]


# --------------------------------------------------------------------------- #
# 3. 缓存
# --------------------------------------------------------------------------- #


async def test_the_catalog_is_cached_within_the_ttl(
    harness: FakeAcpHarness, tmp_path: Path
) -> None:
    """TTL 内不再起第二个子进程——用「站外状态变了但结果没变」来证明。"""
    flag = tmp_path / "logged-in"
    driver = make_driver(_auth_dress(flag), preset=FAKE_PRESET)
    binding = _binding(harness, driver)
    first = await driver.get_model_catalog(binding)
    assert first.degraded is True
    flag.write_text("ok", encoding="utf-8")
    again = await driver.get_model_catalog(binding)
    # 没有重新探测，所以还是那份退化目录（十分钟内引擎只被打扰一次）。
    assert again.degraded is True and again.diagnostics == first.diagnostics
    assert CATALOG_TTL_SECONDS == 600.0


async def test_a_real_session_refreshes_the_cached_catalog(
    harness: FakeAcpHarness, tmp_path: Path
) -> None:
    """真实会话的结果永远覆盖探测快照（它更新）。"""
    flag = tmp_path / "logged-in"
    driver = make_driver(_auth_dress(flag), preset=FAKE_PRESET)
    binding = _binding(harness, driver)
    assert (await driver.get_model_catalog(binding)).models == ()
    flag.write_text("ok", encoding="utf-8")
    await driver.create_native_session(binding, CreateSessionOptions())
    catalog = await driver.get_model_catalog(binding)
    assert [m.model_id for m in catalog.models] == ["fake:small", "fake:large"]
    assert catalog.degraded is False
    assert (await driver.read_auth_state(binding)).state == "signed_in"


# --------------------------------------------------------------------------- #
# 4. 发消息这条路：类型化异常 + 修法
# --------------------------------------------------------------------------- #


async def test_start_runtime_raises_a_typed_auth_error(
    harness: FakeAcpHarness, tmp_path: Path
) -> None:
    driver = make_driver(
        _auth_dress(tmp_path / "logged-in", methods=("terminal-login",)),
        preset=FAKE_PRESET,
    )
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    with pytest.raises(AuthRequiredError) as caught:
        await driver.start_runtime(conversation, "card")
    error = caught.value
    assert error.auth_methods == ("terminal-login",)
    assert error.failure is not None
    assert error.failure.code == AUTH_REQUIRED
    assert error.failure.hint == "在终端运行 `fake-cli login`，然后重试"
    # 失败顺手把登录态记下来：引擎卡不用再去问一次。
    assert (await driver.read_auth_state(binding)).state == "signed_out"


def test_an_unrecognised_rejection_is_not_dressed_up_as_a_login_problem() -> None:
    """认不出来就**不给**修法：编一句「请先登录」会把别的故障导向错误的修法。

    两个反例都来自取证：``-32602`` 的缺参，以及**同样是 -32000** 但说的是
    「API key 没配」的那一句——后者正是这条判据必须比状态码更细的理由。
    """
    driver = make_driver(preset=FAKE_PRESET)
    assert driver._auth_failure(AcpRpcError(-32602, "Invalid params")) is None  # noqa: SLF001
    assert (
        driver._auth_failure(  # noqa: SLF001
            AcpRpcError(-32000, "API key is missing or not configured.")
        )
        is None
    )
    # 只有一句话、没有 data 的那种也要认得出来（七家里有两家是这个形状）。
    recognised = driver._auth_failure(  # noqa: SLF001
        AcpRpcError(-32000, "Authentication required")
    )
    assert recognised is not None and recognised.code == AUTH_REQUIRED


# --------------------------------------------------------------------------- #
# 5. configOptions 里的模式也能用来切审批档
# --------------------------------------------------------------------------- #


async def test_set_mode_falls_back_to_config_options(harness: FakeAcpHarness) -> None:
    """``modes.availableModes`` 整段不存在时，改从 ``configOptions`` 找 modeId。"""
    driver = make_driver(
        {
            "configOptions": True,
            "setMode": True,
            "modes": ["default", "bypassPermissions"],
        },
        preset=FAKE_PRESET,
    )
    project = harness.make_project()
    binding = harness.make_binding(project, driver)
    conversation = harness.make_conversation(project, binding)
    handle = await driver.start_runtime(conversation, "card")
    try:
        assert handle.metadata["acpModeId"] == "default"
        assert driver.warnings == []
    finally:
        await driver.stop_runtime(handle)
