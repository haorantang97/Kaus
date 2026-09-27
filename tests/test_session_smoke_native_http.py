"""`scripts/session_smoke.py --driver native-http` 的自检（批次六）。

这个脚本是**用户在 Mac 上的验收入口**，所以它自己不能是一堆没跑过的代码。这里把
它的主流程对着假网关跑一遍：真的挂 router、真的起本地端口、真的走 SSE，只有最外
面的 `hermes gateway run` 是假的（沙盒里没有 Hermes）。

真机上多出来的那一段——隔离 `HERMES_HOME`、自己拉起网关、跑完收摊——由
`--home-mode isolated` 承担，它复用 `scripts/probe/probelib/sandbox.py` 的护栏，
在这里只做「不带 `--base-url` 时确实会去找 hermes 可执行文件」的断言。

运行：``python3 -m pytest tests/test_session_smoke_native_http.py -q``
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "kernel"
for _path in (str(REPO_ROOT), str(KERNEL_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

from drivers.hermes.testing.fake_api_server import (  # noqa: E402
    FakeHermesApiServer,
    approval_script,
    text_only_script,
)

KEY_ENV = "SESSION_SMOKE_TEST_KEY"


@pytest.fixture(scope="module")
def smoke():
    path = REPO_ROOT / "scripts" / "session_smoke.py"
    spec = importlib.util.spec_from_file_location("session_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def gateway(monkeypatch):
    server = FakeHermesApiServer().start()
    monkeypatch.setenv(KEY_ENV, server.api_key)
    try:
        yield server
    finally:
        server.stop()


def rows_of(printed: str) -> dict[str, str]:
    """从验收表里把 ``key -> 状态`` 抠出来（表就是给人看的，也顺手当断言面）。"""
    found: dict[str, str] = {}
    for line in printed.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("[OK]", "[XX]", "[--]")):
            continue
        parts = stripped.split()
        found[parts[1]] = {"[OK]": "通过", "[XX]": "未通过", "[--]": "未测"}[parts[0]]
    return found


def base_argv(gateway, tmp_path, **extra) -> list[str]:
    argv = [
        "--driver", "native-http",
        "--live",
        "--base-url", gateway.base_url,
        "--home", str(tmp_path / "hermes-home"),
        "--key-env", KEY_ENV,
        "--timeout", "30",
    ]
    for key, value in extra.items():
        flag = "--" + key.replace("_", "-")
        argv.append(flag)
        if value is not True:
            argv.append(str(value))
    return argv


# --------------------------------------------------------------------------- #
# 1. dry-run 与配置翻译
# --------------------------------------------------------------------------- #


def test_dry_run_exits_zero_and_touches_nothing(smoke, capsys):
    assert smoke.main(["--driver", "native-http", "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "不读 .env" in printed
    # 凭据只以**变量名**出现。
    assert smoke.GATEWAY_KEY_ENV in printed


def test_the_backend_entry_only_carries_a_credential_reference(smoke):
    args = smoke.parse_args(["--driver", "native-http", "--key-env", KEY_ENV])
    entry = smoke.backend_config(args)["backends"][0]
    assert entry["driver"] == "native-http"
    assert entry["env_keys"] == [KEY_ENV]
    assert entry["key_ref"] == f"credential-store:{KEY_ENV}"
    assert entry["mode"] == "adopted"


def test_model_key_env_reports_presence_but_never_the_value(smoke, monkeypatch):
    monkeypatch.setenv("SMOKE_MODEL_KEY", "super-secret-value")
    args = smoke.parse_args(["--model-key-env", "SMOKE_MODEL_KEY,ABSENT_KEY"])
    report = smoke.model_key_report(args)
    assert "SMOKE_MODEL_KEY=已设置" in report
    assert "ABSENT_KEY=未设置" in report
    assert "super-secret-value" not in report


# --------------------------------------------------------------------------- #
# 2. 对着假网关跑主流程
# --------------------------------------------------------------------------- #


def test_a_basic_turn_against_the_fake_gateway_passes(smoke, gateway, tmp_path, capsys):
    gateway.set_script(text_only_script)
    code = smoke.main(
        base_argv(gateway, tmp_path, skip_multiturn=True, skip_approval=True)
    )
    printed = capsys.readouterr().out
    rows = rows_of(printed)
    assert code == 0
    assert rows["probe"] == "通过"
    assert rows["session"] == "通过"
    assert rows["turn"] == "通过"
    assert rows["events"] == "通过"
    # 没跑的两项必须如实写「未测」，不许当成通过。
    assert rows["multiturn"] == "未测"
    assert rows["approval"] == "未测"
    assert "version=0.21.0" in printed
    # 网关 key 一个字节都不该出现在输出里。
    assert gateway.api_key not in printed


def test_the_approval_turn_denies_in_real_time(smoke, gateway, tmp_path, capsys):
    """第 3 轮：抓到 permission.requested → 立刻 deny → sentinel 不存在。"""
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return approval_script() if calls["n"] == 2 else text_only_script()

    gateway.set_script(factory)
    code = smoke.main(base_argv(gateway, tmp_path, skip_multiturn=True))
    printed = capsys.readouterr().out
    rows = rows_of(printed)
    assert code == 0, printed
    assert rows["approval"] == "通过"
    assert "已实时拒绝" in printed
    assert "sentinel=未生成" in printed
    assert "permission.requested" in printed and "permission.resolved" in printed


def test_a_gateway_that_is_not_there_fails_loudly(smoke, gateway, tmp_path, capsys):
    argv = base_argv(gateway, tmp_path, skip_multiturn=True, skip_approval=True)
    gateway.stop()
    code = smoke.main(argv)
    printed = capsys.readouterr().out
    assert code == 1
    assert rows_of(printed)["probe"] == "未通过"
    # 网关没起来就别再发回合，免得把失败原因搅在一起。
    assert "POST messages" not in printed


def test_json_output_is_machine_readable(smoke, gateway, tmp_path, capsys):
    import json

    gateway.set_script(text_only_script)
    smoke.main(
        base_argv(gateway, tmp_path, skip_multiturn=True, skip_approval=True, json=True)
    )
    printed = capsys.readouterr().out
    tail = printed.split("== JSON ==", 1)[1]
    payload = json.loads(tail[: tail.rfind("}") + 1])
    assert payload["passed"] is True
    assert {row["key"] for row in payload["rows"]} >= {
        "probe",
        "session",
        "turn",
        "multiturn",
        "approval",
        "reasoning",
        "events",
        "elapsed",
    }
    assert payload["turns"][0]["terminal"] == "run.completed"
    assert payload["turns"][0]["eventCounts"]["message.delta"] == 3


# --------------------------------------------------------------------------- #
# 3. 隔离模式的护栏
# --------------------------------------------------------------------------- #


def test_isolated_mode_needs_a_real_hermes_binary(smoke):
    """没装 Hermes 就明说，而不是憋出一个连不上的超时。"""
    args = smoke.parse_args(
        ["--driver", "native-http", "--hermes-bin", "hermes-does-not-exist"]
    )
    with pytest.raises(RuntimeError) as caught:
        smoke.start_isolated_gateway(args)
    assert "找不到 hermes 可执行文件" in str(caught.value)
    assert "--base-url" in str(caught.value)


def test_the_script_never_reads_dot_env():
    """AD-60：烟测脚本**不得**读取或复制 `.env`。"""
    source = (REPO_ROOT / "scripts" / "session_smoke.py").read_text(encoding="utf-8")
    assert "dotenv" not in source
    reads = ("open(", "read_text", "read_bytes", "Path(", "copy", "shutil")
    for number, line in enumerate(source.splitlines(), start=1):
        if ".env" in line:
            assert not any(token in line for token in reads), f"第 {number} 行读了 .env：{line}"
