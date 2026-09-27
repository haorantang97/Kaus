"""批次三十五（AD-159）第一件：引擎进程没拉起来时，必须有人话 + 修法。

真机代价（批次三十四的 Mac 复验报告）：测试员
一整夜卡在「模型目录是空的」上。根因是 ``npx`` 从 npm 拉下来的那个平台二进制被
截断了，macOS 拒绝执行它（Node 报 errno ``-88``）——也就是说，**进程压根没起来**。
而仪表盘上只看得到一个空目录和一句 ``runtime_start_failed``：既没说是哪条命令、
也没说系统给的理由、更没说下一步该干什么。

本文件守 Driver 侧的四条：

1. 命令不存在 → 类型化的 :class:`~drivers.base.AgentSpawnError`，消息里有
   ``argv[0]`` 与 errno 名；
2. 进程起来了、握手期间就退了（退出码非零 / 退出码为零两种形状都要认）→ 同上，
   并带上它自己 stderr 的第一句；
3. ``probe()`` 把这份人话 + 修法放进 ``probeMessage``（引擎卡读的就是它），
   状态是 ``unavailable``；
4. 目录探测把同一句挂在空目录的 ``diagnostics`` 上，``read_auth_state`` 把它当
   ``hint``——登录态仍是 ``unknown``（进程都没起来，登没登录我们并不知道）。

安全：**任何一条消息里都不许出现环境变量的值**。stderr 是 agent 自己写的，
真机上完全可能带着我们透传给它的环境；本文件最后一条用例就在验证这一点。

隔离：全部跑在假 agent 子进程上（``--dress {"exitOnStart": …}``）与临时目录里，
不碰任何真实引擎、不读任何凭据文件。
"""

from __future__ import annotations

import tempfile

import pytest

from drivers.acp.client import AcpAgentSpec, AcpSpawnError
from drivers.acp.driver import AcpDriver
from drivers.acp.failure_hints import (
    AGENT_SPAWN_FAILED,
    SPAWN_HINT,
    scrub_env_values,
    stderr_excerpt,
)
from drivers.acp.testing.fake_acp_agent import fake_agent_spec
from drivers.acp.testing.harness import FakeAcpHarness
from drivers.base import AgentSpawnError

#: 一个**保证不存在**的可执行文件名。不用 ``/bin/nope`` 之类，是为了让断言里的
#: 那个字符串一眼看出它是故意的。
MISSING_COMMAND = "kaus-there-is-no-such-binary-35"


@pytest.fixture
def harness() -> FakeAcpHarness:
    return FakeAcpHarness()


def make_driver(dress: dict | None = None, *, command: tuple[str, ...] | None = None):
    workdir = tempfile.gettempdir()
    spec = (
        AcpAgentSpec(command=command, cwd=workdir)
        if command is not None
        else fake_agent_spec("text-stream", cwd=workdir, dress=dress)
    )
    return AcpDriver(
        spec,
        backend_key="acp",
        default_cwd=workdir,
        call_timeout=20.0,
        prompt_timeout=30.0,
    )


# --------------------------------------------------------------------------- #
# 1. 命令根本不存在
# --------------------------------------------------------------------------- #


async def test_missing_command_raises_a_typed_spawn_error_naming_argv0() -> None:
    """``ENOENT``：说清是哪个可执行文件不存在，并给出那句修法。"""
    driver = make_driver(command=(MISSING_COMMAND,))
    with pytest.raises(AgentSpawnError) as caught:
        await driver._connect()
    failure = caught.value.failure
    assert failure is not None
    assert failure.code == AGENT_SPAWN_FAILED
    assert MISSING_COMMAND in failure.message
    assert "ENOENT" in failure.message
    assert failure.hint == SPAWN_HINT
    # ACP 内部照旧接得住它（既有的 except AcpTransportError 不能因此漏掉）。
    assert isinstance(caught.value, AcpSpawnError)


# --------------------------------------------------------------------------- #
# 2. 进程起来了，但没活到握手结束
# --------------------------------------------------------------------------- #


async def test_exit_during_handshake_reports_exit_code_and_first_stderr_line() -> None:
    """非零退出：退出码 + 它自己 stderr 的第一句，都要出现在人话里。"""
    driver = make_driver(
        {"exitOnStart": 3, "stderrOnStart": "dyld: bad executable\nsecond line"}
    )
    with pytest.raises(AgentSpawnError) as caught:
        await driver._connect()
    failure = caught.value.failure
    assert failure is not None
    assert failure.code == AGENT_SPAWN_FAILED
    assert "exit 3" in failure.message
    assert "dyld: bad executable" in failure.message
    # 只摘第一行：一份栈搬上 wire 只会让提示自己变成一堵墙。
    assert "second line" not in failure.message


async def test_clean_exit_without_answering_initialize_is_still_a_spawn_failure() -> None:
    """退出码 0 也算失败：「成功地什么都没做就走了」是更容易误诊的那一种。"""
    driver = make_driver({"exitOnStart": 0})
    with pytest.raises(AgentSpawnError) as caught:
        await driver._connect()
    assert caught.value.failure is not None
    assert "initialize" in caught.value.failure.message


# --------------------------------------------------------------------------- #
# 3. 探测面：probeState / probeMessage
# --------------------------------------------------------------------------- #


async def test_probe_reports_unavailable_with_the_hint_in_the_message() -> None:
    """引擎卡上那一行：``probeState=unavailable``，``probeMessage`` = 人话 + 修法。"""
    driver = make_driver(command=(MISSING_COMMAND,))
    result = await driver.probe()
    assert result.state == "unavailable"
    assert result.installed is False
    assert result.message is not None
    assert MISSING_COMMAND in result.message
    assert SPAWN_HINT in result.message


# --------------------------------------------------------------------------- #
# 4. 目录探测与登录态
# --------------------------------------------------------------------------- #


async def test_empty_catalog_carries_the_spawn_diagnostic(
    harness: FakeAcpHarness,
) -> None:
    """空目录不许只是空的：为什么空、怎么修，都挂在 ``diagnostics`` 上。"""
    driver = make_driver(command=(MISSING_COMMAND,))
    binding = harness.make_binding(harness.make_project(), driver)
    catalog = await driver.get_model_catalog(binding)
    assert catalog.models == ()
    assert catalog.degraded is True
    assert len(catalog.diagnostics) == 1
    assert MISSING_COMMAND in catalog.diagnostics[0]
    assert SPAWN_HINT in catalog.diagnostics[0]


async def test_auth_state_stays_unknown_but_gains_a_hint(
    harness: FakeAcpHarness,
) -> None:
    """进程没起来 ≠ 没登录：状态仍是 ``unknown``，但 ``hint`` 说得出人话。"""
    driver = make_driver(command=(MISSING_COMMAND,))
    binding = harness.make_binding(harness.make_project(), driver)
    auth = await driver.read_auth_state(binding)
    assert auth.state == "unknown"
    assert auth.model == "own-auth"
    assert auth.hint is not None and SPAWN_HINT in auth.hint


# --------------------------------------------------------------------------- #
# 5. 安全：环境变量的值一个字都不上 wire
# --------------------------------------------------------------------------- #


def test_stderr_excerpt_scrubs_environment_values() -> None:
    """agent 把我们透传的环境打进了 stderr —— 挂出去之前必须换成 ``<env:NAME>``。"""
    environ = {"SOME_TOKEN": "s3cr3t-value-abcdef", "TZ": "UTC"}
    line = "failed to start with SOME_TOKEN=s3cr3t-value-abcdef in TZ=UTC"
    excerpt = stderr_excerpt(line, environ=environ)
    assert "s3cr3t-value-abcdef" not in excerpt
    assert "<env:SOME_TOKEN>" in excerpt
    # 太短的值不参与替换：一个 ``TZ=UTC`` 不该把正文里所有的 UTC 都吃掉。
    assert "TZ=UTC" in excerpt


def test_scrubbing_replaces_the_longest_value_first() -> None:
    """嵌套前缀：先换长的，否则短的会把长的啃掉一半。"""
    environ = {"HOME": "/home/someone", "CACHE": "/home/someone/.cache"}
    scrubbed = scrub_env_values("open /home/someone/.cache/x", environ)
    assert scrubbed == "open <env:CACHE>/x"


async def test_spawn_message_never_contains_an_environment_value() -> None:
    """端到端：假 agent 把一个环境变量的值打进 stderr，人话里不许出现它。"""
    secret = "kaus-batch35-secret-value"
    driver = make_driver(
        {"exitOnStart": 1, "stderrOnStart": f"boom {secret}"},
    )
    driver.agent_spec = driver.agent_spec.with_env(KAUS_TEST_FAKE_TOKEN=secret)
    with pytest.raises(AgentSpawnError) as caught:
        await driver._connect()
    failure = caught.value.failure
    assert failure is not None
    assert secret not in failure.message
    assert "<env:KAUS_TEST_FAKE_TOKEN>" in failure.message
