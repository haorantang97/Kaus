"""批次十九第 1、2 件（Driver 侧）：登录状态三态判定与原生会话计数。

纪律同批次十六/十七/十八：只用假 gateway（``fake_api_server``）与**临时**
``HERMES_HOME``，一行都不碰真实的引擎家目录。本文件里出现的每一个「凭据」
都是夹具当场写进临时目录的假串。

安全侧的取证点（这批的核心）
----------------------------
``test_key_presence_check_never_reads_the_value`` 与
``test_no_credential_value_leaks_into_the_state_or_the_log`` 是两条**红线用例**：
前者证明键名存在性判定连文件里的值都没取（把值换掉，判定结果一模一样），
后者证明那个值不出现在任何返回对象、任何日志行里。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from drivers.hermes import credentials
from drivers.hermes.testing.harness import FakeHermesHarness


@pytest.fixture()
def rig(tmp_path: Path):
    harness = FakeHermesHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


async def _ready(rig):
    driver = rig.make_driver()
    project = rig.make_project()
    binding = rig.make_binding(project, driver)
    await driver.probe()
    return driver, project, binding


# --------------------------------------------------------------------------- #
# 第 1 件：三态判定
# --------------------------------------------------------------------------- #


async def test_signed_in_when_the_ref_resolves_and_the_gateway_accepts_it(rig) -> None:
    """引用指得到 + 网关没回 401 → ``signed_in``。"""
    driver, _project, binding = await _ready(rig)

    state = await driver.read_auth_state(binding)

    assert state.state == "signed_in"
    assert state.model == "managed-credential"
    # 这台引擎没有账号概念——编一个出来只会让界面多一行假信息。
    assert state.account is None
    assert state.checked_at is not None
    assert state.hint is None


async def test_signed_out_when_the_gateway_rejects_the_key(rig) -> None:
    """网关回 401 → ``signed_out``，并带上「怎么修」。"""
    driver, _project, binding = await _ready(rig)
    # 网关那边换了 key：我们手上这把从此不被认。
    rig.servers["default"].api_key = "rotated-" + rig.servers["default"].api_key
    driver.supervisor_for(binding).invalidate()

    state = await driver.read_auth_state(binding)

    assert state.state == "signed_out"
    assert state.hint  # 401 是认得出的根因，必须给下一步动作
    assert state.account is None


async def test_unknown_when_the_gateway_is_offline(rig) -> None:
    """网关离线 → ``unknown``，**不是** ``signed_out``。

    连不上不等于没登录。报成未登录会把用户支去修一个根本没坏的东西
    （他真正要做的是把网关跑起来），所以这一档必须是「问不出来」。
    """
    driver, _project, binding = await _ready(rig)
    rig.servers["default"].stop()
    driver.supervisor_for(binding).invalidate()

    state = await driver.read_auth_state(binding)

    assert state.state == "unknown"
    assert state.hint  # 「先把网关跑起来」是认得出的修法


async def test_signed_out_when_the_key_ref_points_at_nothing(rig) -> None:
    """引用指不到东西（``.env`` 里没有这个键名）→ ``signed_out``。"""
    driver, project, binding = await _ready(rig)
    home = Path(binding.runtime_config["api_server"]["hermes_home"])
    # 键名换掉：文件还在、值还在，但我们要找的那个键名不在了。
    (home / ".env").write_text("API_SERVER_ENABLED=true\n", encoding="utf-8")
    driver.supervisor_for(binding).invalidate()

    state = await driver.read_auth_state(binding)

    assert state.state == "signed_out"


# --------------------------------------------------------------------------- #
# 红线：只判键名，绝不取值
# --------------------------------------------------------------------------- #


def test_key_presence_check_never_reads_the_value(tmp_path: Path) -> None:
    """判定只看键名——把值整个换掉，结论一模一样。

    这条用例的意义不在「结果对」，在**它证明了值没有参与判定**：如果实现里
    偷偷取了值（比如「值非空才算有」），换值就会改变结论。
    """
    env = tmp_path / ".env"
    env.write_text("API_SERVER_KEY=first-secret\nOTHER=1\n", encoding="utf-8")
    assert credentials.env_key_present(env, "API_SERVER_KEY") is True

    env.write_text("API_SERVER_KEY=totally-different\n", encoding="utf-8")
    assert credentials.env_key_present(env, "API_SERVER_KEY") is True

    # 值空着也算「这个键名在」——键名存在性与值有没有内容是两个问题。
    env.write_text("API_SERVER_KEY=\n", encoding="utf-8")
    assert credentials.env_key_present(env, "API_SERVER_KEY") is True

    # 键名不在就是不在。
    env.write_text("SOMETHING_ELSE=x\n", encoding="utf-8")
    assert credentials.env_key_present(env, "API_SERVER_KEY") is False
    # 文件不存在 = 指不到，不是异常。
    assert credentials.env_key_present(tmp_path / "nope.env", "API_SERVER_KEY") is False


def test_key_presence_ignores_comments_and_accepts_export_form(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# API_SERVER_KEY=commented-out\nexport API_SERVER_KEY=x\n", encoding="utf-8"
    )
    assert credentials.env_key_present(env, "API_SERVER_KEY") is True

    env.write_text("# API_SERVER_KEY=commented-out\n", encoding="utf-8")
    assert credentials.env_key_present(env, "API_SERVER_KEY") is False


def test_credential_store_ref_only_checks_the_environment_variable_name() -> None:
    """``credential-store:NAME`` 只判这个**名字**在不在环境里，不取它的值。"""
    environ = {"KAUS_TEST_KEY": "s3cret"}

    assert credentials.credential_ref_present(
        "credential-store:KAUS_TEST_KEY", environ=environ
    ) is True
    assert credentials.credential_ref_present(
        "credential-store:MISSING", environ=environ
    ) is False
    # 认不出的形态与空引用一律算「指不到」。
    assert credentials.credential_ref_present("nonsense:x", environ=environ) is False
    assert credentials.credential_ref_present(None, environ=environ) is False


async def test_no_credential_value_leaks_into_the_state_or_the_log(
    rig, caplog
) -> None:
    """凭据值不得出现在返回对象里，也不得出现在任何一行日志里。"""
    driver, _project, binding = await _ready(rig)
    secret = rig.servers["default"].api_key
    assert secret  # 夹具确实给了一把（假）key

    with caplog.at_level(logging.DEBUG):
        state = await driver.read_auth_state(binding)
        count = await driver.count_native_sessions(binding)

    assert secret not in repr(state)
    assert secret not in repr(state.model_dump(mode="json", by_alias=True))
    assert secret not in repr(count)
    for record in caplog.records:
        assert secret not in record.getMessage()


# --------------------------------------------------------------------------- #
# 第 2 件：原生会话计数
# --------------------------------------------------------------------------- #


async def test_count_matches_the_visible_session_list(rig) -> None:
    """口径与 ``list_native_sessions`` 逐字一致：hidden / archived 的不数。"""
    driver, _project, binding = await _ready(rig)
    server = rig.servers["default"]
    server.seed_session(hidden=False)
    server.seed_session(hidden=False)
    server.seed_session(hidden=True)
    server.seed_session(archived=True)

    listed = await driver.list_native_sessions(binding)
    count = await driver.count_native_sessions(binding)

    assert count == len(listed) == 2


async def test_count_is_none_when_the_gateway_is_offline(rig) -> None:
    """数不出来就是 ``None``——返回 0 会被界面渲染成「0 条会话」，那是谎话。"""
    driver, _project, binding = await _ready(rig)
    rig.servers["default"].stop()
    driver.supervisor_for(binding).invalidate()

    assert await driver.count_native_sessions(binding) is None


# --------------------------------------------------------------------------- #
# 能力声明（第 4 件在 Driver 侧的落点）
# --------------------------------------------------------------------------- #


async def test_capabilities_declare_the_auth_axis(rig) -> None:
    driver, _project, _binding = await _ready(rig)

    capabilities = await driver.get_capabilities()

    assert capabilities.auth.model == "managed-credential"
    assert capabilities.auth.state_reporting.value == "supported"
    assert bool(capabilities.auth.state_reporting) is True
