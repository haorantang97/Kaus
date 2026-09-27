"""``drivers/acp/`` 的纯净性断言：它是**通用** Driver，不许认识任何一家 Agent。

为什么单独有这一条
------------------
``kernel/tests/test_public_type_purity.py`` 只扫 ``app/`` / ``runtime/`` 与
``drivers/{base,registry,__init__}.py``；``drivers/<backend>/`` 天然在它的扫描
范围之外——因为专用 Driver **就是**知道某一家私有协议的地方。

但 ACP Driver 不是专用 Driver。它对应 baseline §9.2 的**路径 A**：一个协议、
多家 Agent。一旦它开始按某家的名字写分支，它就退化成了那家的专用 Driver，而
「新 Agent 优先走 ACP」这条接入规则也就随之作废。所以这里把同一套机械断言按
更严的口径（含 ``drivers/acp/tests`` 与 ``drivers/acp/testing``）再跑一遍。

允许出现的是**协议名**（``acp``）与**能力名**，不是产品名。实测得来的行为差异
必须以能力/形状描述，例如「某些 agent 的 ``session/load`` 参数校验永远失败」，
而不是「X 家的 load 不能用」。

唯一的豁免：``presets.py``（AD-151）
------------------------------------
预设目录**就是**一张产品清单——把产品名从它里面删掉，这张表就没有内容了。
所以它在产品名扫描里被豁免，代价是换一条**更严**的断言顶上
（:func:`test_no_preset_id_leaks_into_the_driver`）：除了目录自己，本包里任何
文件都不许出现任何预设 id 的字面量。这正是 AD-151 想守的那条分工——怪癖只进表，
不进 ``if``；Driver 读的是 :class:`~drivers.acp.presets.AgentQuirks` 的字段，
不是 agent 的名字。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ACP_PACKAGE = Path(__file__).resolve().parent.parent

#: 受禁的产品/私有真相名。与 ``tests/test_public_type_purity.py`` 同源，
#: 外加本包自己的理由：它们都是「某一家」，而这个 Driver 服务于全体。
FORBIDDEN_TOKENS: dict[str, str] = {
    "hermes": r"hermes",
    "profile": r"profile",
    "codex": r"codex",
    "claude": r"claude",
    "gemini": r"gemini",
    "xterm": r"xterm",
    "pty": r"(?<![a-z])pty(?![a-z])",
}


#: 扫描器自己必须排除：它按定义就要写出这些词才能去找它们。
SELF = Path(__file__).resolve()

#: AD-151 的豁免名单：预设目录与只读它的那份测试。见模块 docstring。
CATALOG = ACP_PACKAGE / "presets.py"
CATALOG_TEST = ACP_PACKAGE / "tests" / "test_presets.py"
EXEMPT: frozenset[Path] = frozenset({SELF, CATALOG.resolve(), CATALOG_TEST.resolve()})


def acp_source_files() -> list[Path]:
    return sorted(
        p
        for p in ACP_PACKAGE.rglob("*.py")
        if "__pycache__" not in p.parts and p.resolve() not in EXEMPT
    )


def test_the_package_is_actually_scanned() -> None:
    names = {p.name for p in acp_source_files()}
    assert {"client.py", "translator.py", "capabilities.py", "driver.py"} <= names
    assert "fake_acp_agent.py" in names, "假 agent 也要受同一条约束"
    assert "harness.py" in names


@pytest.mark.parametrize("path", acp_source_files(), ids=lambda p: p.name)
def test_no_vendor_specific_token_in_acp_driver(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = {
        token: len(re.findall(pattern, text, flags=re.IGNORECASE))
        for token, pattern in FORBIDDEN_TOKENS.items()
        if re.search(pattern, text, flags=re.IGNORECASE)
    }
    assert not hits, (
        f"{path.relative_to(ACP_PACKAGE.parent.parent)} 出现了具体 Agent 的私有词 {hits}；"
        "通用 ACP Driver 只能按协议与能力说话（baseline §9.2 路径 A）"
    )


def test_fake_agent_name_is_neutral() -> None:
    from drivers.acp.testing.fake_acp_agent import AGENT_NAME

    assert AGENT_NAME == "fake-acp-agent"
    for token in FORBIDDEN_TOKENS:
        assert token not in AGENT_NAME.lower()


def test_no_preset_id_leaks_into_the_driver() -> None:
    """AD-151：怪癖只进表，不进 ``if``。

    Driver / client / translator / capabilities / 假 agent 里出现任何一个预设 id 的
    字面量，就说明有人开始按 agent 的名字写分支了——那正是「通用 ACP Driver」退化
    成某一家专用 Driver 的第一步。
    """
    from drivers.acp.presets import preset_ids

    offenders: dict[str, list[str]] = {}
    for path in acp_source_files():
        text = path.read_text(encoding="utf-8")
        # 找的是**字符串字面量**：``pi`` 这种两字母 id 在自然文本里到处都是，
        # 裸子串匹配只会制造假阳性。写成 if 分支的样子必然是个字面量。
        hits = [
            pid
            for pid in preset_ids()
            if re.search(rf"[\"']{re.escape(pid)}[\"']", text)
        ]
        if hits:
            offenders[str(path.relative_to(ACP_PACKAGE.parent.parent))] = hits
    assert not offenders, (
        f"这些文件里出现了预设 id {offenders}；预设 id 只允许出现在 presets.py 里，"
        "Driver 只能读 AgentQuirks 的字段做分支（AD-151）"
    )


def test_catalog_is_actually_exempted_not_missing() -> None:
    """豁免名单指向的文件必须真的存在——否则这条豁免会静默变成「什么都没扫」。"""
    assert CATALOG.exists()
    assert CATALOG.resolve() not in {p.resolve() for p in acp_source_files()}


def test_extension_namespace_is_the_protocol_not_a_product() -> None:
    from drivers.acp.translator import ACP_EXTENSION_NAMESPACE

    assert ACP_EXTENSION_NAMESPACE == "acp"
