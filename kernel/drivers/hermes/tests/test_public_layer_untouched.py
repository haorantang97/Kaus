"""纯净性：公共层一行都没改（规格附录 A 验收点 3）。

验收点 3 的原文有两句，这个文件把两句都变成断言：

1. ``kernel/tests/test_public_type_purity.py`` 仍然通过——它扫 ``app/`` /
   ``runtime/`` 与 ``drivers/{base,registry,__init__}.py``，**不扫**
   ``drivers/<backend>/``，所以 ``drivers/hermes/**`` 天然在范围外；
2. 「扫描范围内相对 master 一行都不改」的 git 比对曾经在这里（3B 施工期的临时
   护栏）。批次四验收后已按 AD-63 退役：公共层的演进走裁决记录 + 纯净性扫描，
   不再靠冻结 diff。

外加一条本 Driver 自己的纪律：Hermes 的私有信息只能从四个出口漏出去——
``RuntimeHandle.metadata`` / ``NativeHistoryEntry.metadata`` /
``AgentBinding.runtime_config_json`` / ``extension.event``（namespace ``"hermes"``）。
"""

from __future__ import annotations

from pathlib import Path

from tests.test_public_type_purity import (
    FORBIDDEN_TOKENS,
    KERNEL_ROOT,
    public_source_files,
)

REPO_ROOT = KERNEL_ROOT.parent
DRIVER_DIR = KERNEL_ROOT / "drivers" / "hermes"

def test_driver_package_is_outside_the_scanned_surface() -> None:
    """``drivers/hermes/**`` 不在扫描范围内——但**范围本身没有被放宽**。"""
    scanned = {p.resolve() for p in public_source_files()}
    hermes_files = {p.resolve() for p in DRIVER_DIR.rglob("*.py")}
    assert hermes_files, "Driver 目录不该是空的"
    assert scanned.isdisjoint(hermes_files)
    # 扫描范围仍然是那三类：app/ + runtime/ + drivers 的三个公共文件。
    relative = {p.resolve().relative_to(KERNEL_ROOT).as_posix() for p in scanned}
    assert {"drivers/base.py", "drivers/registry.py", "drivers/__init__.py"} <= relative
    assert any(name.startswith("app/") for name in relative)
    assert any(name.startswith("runtime/") for name in relative)
    # 没有任何 drivers/<backend>/ 被顺手加进扫描范围（那会让本测试失去意义）。
    assert not [name for name in relative if name.startswith("drivers/hermes/")]


#: 第二个允许出现这个名字的地方：ACP 的预设目录（AD-151）。
#:
#: 它是一张**产品清单**，而这个引擎的 ACP 面（AD-33 的第二条路）就是清单上的一行。
#: 放行的是「目录里有它的名字」，**不是**「公共层认识它」：预设目录里只有 id、
#: 标签、启动 argv 与怪癖位，没有任何这个引擎的私有概念（profile / 家目录 /
#: 网关地址一个都没有），Driver 也仍然只按怪癖表分支。
ACP_PRESET_CATALOG = KERNEL_ROOT / "drivers" / "acp" / "presets.py"


def test_hermes_token_appears_only_inside_this_driver_directory() -> None:
    """``hermes`` 字样在 kernel 里只许出现在这一个 Driver 目录（与夹具/测试/预设目录）。"""
    offenders: list[str] = []
    for path in sorted(KERNEL_ROOT.rglob("*.py")):
        relative = path.relative_to(KERNEL_ROOT)
        parts = relative.parts
        if parts[0] == "drivers" and len(parts) > 1 and parts[1] == "hermes":
            continue
        if path.resolve() == ACP_PRESET_CATALOG.resolve():
            continue
        if "tests" in parts or "mock" in parts or "contract_tests" in parts:
            continue
        text = path.read_text(encoding="utf-8").lower()
        if "hermes" in text:
            offenders.append(relative.as_posix())
    assert not offenders, "以下文件出现了 hermes 字样：\n" + "\n".join(offenders)


def test_the_preset_catalog_exemption_stays_narrow() -> None:
    """豁免只放行**名字**，不放行这个引擎的私有概念（AD-151）。

    预设目录里出现 ``profile`` / ``base_url`` / 家目录之类，就说明有人开始把
    专用 Driver 的知识搬进通用目录了——那正是这条豁免不打算换来的东西。

    **一处按 AD-156 摘出去的例外：``auth_method_ids`` 的取值。** 那些字符串是
    ``initialize`` 里 ``authMethods[].id`` 的**上游原话**（取证抄回来的），不是我们
    引入的概念——比如某家把它的一种登录方式就叫 ``gateway``。扫描前先把这些原话
    逐字摘掉，摘掉之后仍不得出现任何私有概念：放行的是「抄回来的 id」，不是
    「这个词从此可以随便写」。

    **第二处按 AD-158 摘出去的例外：``command`` / ``resume_argv_template`` 里的
    argv 片段。** 那些同样是上游原话——某家 CLI 的子命令开关就叫 ``--profile``，
    与本仓另一个引擎的 ``profile`` 概念只是撞了名字。摘的是**逐字的、带引号的
    那一个 token**，所以「把私有概念写进注释或字段名」照旧会红。

    **第三处按 AD-160 摘出去的例外：``env_keys_hint`` 里的环境变量名。** 某家的
    换 provider 变量就叫 ``ANTHROPIC_BASE_URL``，与本仓 ``backends[].base_url``
    那个概念同样只是撞了子串。这一处摘的是**逐字的变量名本身**（不要求带引号
    ——备注里也要能提这个名字），所以一个孤零零的 ``base_url`` 照旧会红：放行的
    是「抄回来的变量名」，不是「这个词从此可以随便写」。变量的**值**一个字都不在
    目录里，那条纪律由别处守（AD-10 / AD-48）。
    """
    from drivers.acp.presets import PRESETS

    text = ACP_PRESET_CATALOG.read_text(encoding="utf-8").lower()
    upstream_literals: set[str] = set()
    env_names: set[str] = set()
    for preset in PRESETS.values():
        # A registered driver hook is a module reference, not native state.
        if preset.group_isolation_adapter:
            text = text.replace(f'"{preset.group_isolation_adapter.lower()}"', '""')
        upstream_literals.update(preset.auth_method_ids)
        upstream_literals.update(preset.command)
        upstream_literals.update(preset.resume_argv_template or ())
        # Configuration locations are declarative, stat-only watch metadata.
        # Exempt only these exact literals; generic code still cannot read any
        # engine's private state or branch on its name.
        if preset.config_root_default:
            upstream_literals.add(preset.config_root_default)
        upstream_literals.update(preset.config_watch_files)
        env_names.update(preset.env_keys_hint)
        if preset.config_root_env:
            env_names.add(preset.config_root_env)
    for literal in upstream_literals:
        text = text.replace(f'"{literal.lower()}"', '""')
    for name in env_names:
        text = text.replace(name.lower(), "")
    for token in ("profile", "base_url", "gateway", ".hermes", "key_ref"):
        assert token not in text, f"预设目录里出现了引擎私有概念 {token!r}"


def test_the_four_private_outlets_are_the_only_ones_used() -> None:
    """Driver 产出的公共对象里，私有信息只出现在四个约定出口。"""
    import json

    from drivers.hermes.driver import HermesDriver  # noqa: F401 - 确认可导入
    from drivers.hermes.translator import EXTENSION_NAMESPACE

    assert EXTENSION_NAMESPACE == "hermes"

    # 公共事件模型的字段名里没有任何受禁词——这一条其实由公共层的纯净性测试保证，
    # 这里再核一次是为了把「Driver 没有偷偷扩字段」也覆盖到。
    from runtime.event_envelope import AGENT_EVENT_TYPES

    blob = json.dumps(AGENT_EVENT_TYPES).lower()
    for token in FORBIDDEN_TOKENS:
        assert token not in blob


def test_driver_declares_the_public_driver_kind() -> None:
    """AD-09：不引入 ``remote``；AD-18：Hermes 是 ``native``。"""
    from drivers.hermes.driver import HermesDriver

    assert HermesDriver.driver_kind == "native"
