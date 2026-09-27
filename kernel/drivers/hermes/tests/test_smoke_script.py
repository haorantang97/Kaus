"""``scripts/hermes_http_smoke.py`` 的自检。

冒烟脚本是**用户侧真机验证入口**，所以它自己不能是一堆没跑过的代码。这里做两件事：

1. 把它的主流程（``_run_all``）指向假 API server 跑一遍——除了「真的调用模型」
   之外的每一步都真的执行；
2. 断言 ``--check`` 子项与规格 §8 的编号一一对上，且需要 ``--live`` 的那几项
   在没有 ``--live`` 时**明确跳过**而不是偷偷跑掉。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from drivers.hermes.testing.fake_api_server import FakeHermesApiServer

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "hermes_http_smoke.py"


@pytest.fixture(scope="module")
def smoke():
    if not SCRIPT.exists():  # pragma: no cover - 仓库结构变了才会到这
        pytest.skip(f"找不到冒烟脚本：{SCRIPT}")
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "probe"))
    spec = importlib.util.spec_from_file_location("hermes_http_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 先注册再 exec：模块里的 dataclass 会用 sys.modules[cls.__module__] 解析注解，
    # 不注册的话 @dataclass 直接在导入时炸。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeSandbox:
    """只提供 ``_run_all`` 真正用到的那几个面。"""

    def __init__(self, home: Path, api_key: str) -> None:
        self.home = home
        self.workdir = home.parent / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key
        self.notes: list[str] = []

    @property
    def state_db(self) -> Path:
        return self.home / "state.db"

    def env(self, extra: dict | None = None) -> dict:
        return {"HERMES_HOME": str(self.home), **(extra or {})}

    def try_cleanup(self) -> list[str]:
        return []

    def cleanup_instructions(self) -> list[str]:
        return []


def test_check_items_cover_every_open_spec_item(smoke) -> None:
    """``--check`` 的每一项都对上规格 §8 的一个编号。"""
    numbers = {"①", "②", "③", "④", "⑤", "⑦", "⑧"}
    covered = {
        char
        for title in smoke.CHECK_SPEC_ITEMS.values()
        for char in title
        if char in numbers
    }
    assert covered == numbers, f"§8 的这些项没有对应的 --check 子项：{numbers - covered}"
    # 需要真回合的那几项必须标出来，不能在没有 --live 时偷偷跑。
    assert smoke.LIVE_ONLY_CHECKS <= set(smoke.CHECK_SPEC_ITEMS)
    for key in ("multiturn", "approval", "reasoning"):
        assert key in smoke.LIVE_ONLY_CHECKS


def test_unknown_check_is_rejected(smoke) -> None:
    args = smoke.parse_args(["--check", "no-such-item"])
    import asyncio

    assert asyncio.run(smoke.main_async(args)) == 2


async def test_main_flow_runs_against_the_fake_gateway(smoke, tmp_path: Path) -> None:
    server = FakeHermesApiServer().start()
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        f"API_SERVER_ENABLED=true\nAPI_SERVER_KEY={server.api_key}\n", encoding="utf-8"
    )
    sandbox = _FakeSandbox(home, server.api_key)

    from drivers.hermes.driver import HermesDriver
    from drivers.hermes.supervisor import GatewayConfig

    driver = HermesDriver(
        hermes_root=home.parent,
        default_gateway=GatewayConfig(
            hermes_home=home,
            port=server.port,
            key_ref=f"hermes-env:{home}/.env#API_SERVER_KEY",
            mode="adopted",
        ),
        # 故意给一个不存在的可执行文件：带外检测那一步必须**优雅降级**成「未测」，
        # 而不是让整个冒烟崩掉。
        hermes_bin="hermes-does-not-exist",
    )
    report = smoke.Report()
    args = argparse.Namespace(live=False)
    try:
        code = await smoke._run_all(  # noqa: SLF001 - 就是要测这个内部主流程
            args, {"shapes"}, report, driver, sandbox, server.port, "hermes-does-not-exist"
        )
    finally:
        server.stop()

    keys = {item.key: item for item in report.items}
    assert keys["probe"].state == smoke.MEASURED
    assert keys["session"].state == smoke.MEASURED
    assert keys["session"].detail.startswith("api_")
    assert keys["history"].state == smoke.MEASURED
    assert keys["cli"].detail.startswith("hermes-does-not-exist -p default chat --resume ")
    # 不需要 --live 的 §8-⑦ 形状项应该真的跑了。
    assert keys["shapes"].state == smoke.MEASURED
    # 没有 --live → 不发起模型回合，如实记成未测。
    assert keys["turn"].state == smoke.NOT_TESTED
    assert "--live" in keys["turn"].detail
    # hermes 不存在 → 带外那一步降级成未测，不崩。
    assert keys["oob"].state == smoke.NOT_TESTED
    assert code == 0


async def test_live_only_checks_are_skipped_without_live(smoke, tmp_path: Path) -> None:
    report = smoke.Report()
    args = argparse.Namespace(live=False)
    await smoke._run_check(  # noqa: SLF001
        "multiturn", args, report, None, None, None, None, "hermes", 0
    )
    item = report.items[-1]
    assert item.state == smoke.NOT_TESTED
    assert "--live" in item.detail
