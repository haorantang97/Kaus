"""``probe_adapter --mcp-ping`` 这条路本身要能跑（批次四十三第 5 件）。

取证工具最容易出的事，是它自己坏了而没人发现——真机上跑出一个
``verified_false``，我们分不清是「那家 agent 不挂 MCP」还是「探针自己没把
MCP 传进去」。所以这里把整条路在**假 ACP agent + 假 MCP 服务**上跑一遍：

- 会调工具的 agent → ``verified_true``，两条判据都成立；
- 不会调工具的 agent（普通文本剧本）→ ``verified_false``，且**把它的原话记下来**；
- 输出里不许出现任何环境变量的值，也不许出现本机路径。

这是真子进程，不 mock：探针拉起 agent，agent 再拉起 MCP 服务，两层 JSON-RPC。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from drivers.acp.testing.fake_acp_agent import FAKE_AGENT_PATH
from drivers.acp.testing.fake_mcp_server import TOOL_NAME
from drivers.acp.testing.probe_adapter import probe

#: 假 agent 需要它才 import 得到本仓的模块（它要拉起假 MCP 服务）。
KERNEL_ROOT = str(FAKE_AGENT_PATH.parents[3])


def _run(scenario: str, *, mcp_ping: bool = True, prompt: bool = False) -> dict:
    # 探针的子进程环境是白名单式的：假 agent 要 import 本仓，得把 PYTHONPATH
    # 按名字点进去（值仍然从本进程环境取，探针自己一个字都不打印）。
    previous = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = KERNEL_ROOT
    try:
        return probe(
            [sys.executable, str(FAKE_AGENT_PATH), "--scenario", scenario],
            env_keys=("PYTHONPATH",),
            mcp_ping=mcp_ping,
            prompt=prompt,
            init_timeout=30.0,
            session_timeout=30.0,
            prompt_window=45.0,
        )
    finally:
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous


@pytest.fixture(scope="module")
def pinged() -> dict:
    return _run("mcp-ping")


def test_the_probe_reaches_a_verdict_on_an_agent_that_really_calls_the_tool(
    pinged: dict,
) -> None:
    assert pinged["sessionNew"]["outcome"] == "ok"
    verdict = pinged["mcpPing"]
    assert verdict["bit"] == "mcp_via_session_new"
    assert verdict["verdict"] == "verified_true"
    # 两条判据本来任一成立即可；在这台假 agent 上**两条都**该成立——
    # 它既报了工具调用，也把工具的原话复述了出来。
    assert verdict["toolCallSeen"] is True
    assert verdict["nonceEchoed"] is True
    # 判过了就不必留一整段原话。
    assert verdict["agentSaidVerbatim"] is None


def test_the_verdict_is_false_and_quotes_the_agent_when_it_never_calls_the_tool() -> None:
    """普通文本剧本 = 一台「收下了 mcpServers 但什么都没挂」的 agent。"""
    report = _run("text-stream")
    verdict = report["mcpPing"]
    assert verdict["verdict"] == "verified_false"
    assert verdict["toolCallSeen"] is False and verdict["nonceEchoed"] is False
    # 原话必须留下来——那是唯一能解释「为什么不行」的材料（D-03）。
    assert verdict["agentSaidVerbatim"]


def test_the_report_carries_names_and_the_nonce_but_no_values(pinged: dict) -> None:
    """红线：取证输出里只有名字。

    ``mcpPing`` 那一段只报 server 名字与本次随机 nonce；本机的 python 路径、
    假 MCP 服务的文件路径一律不进去——那是「这台机器长什么样」，不是 agent 说的话。
    """
    section = json.dumps(pinged["mcpPing"], ensure_ascii=False)
    assert section.count(TOOL_NAME) >= 0  # 工具名出现与否都合法，它不是密文
    assert sys.executable not in section
    assert str(FAKE_AGENT_PATH) not in section
    assert "fake_mcp_server" not in section
    # 透传过的变量仍然只以**名字**形态出现（探针原有的口径，这里再核一次）。
    assert "PYTHONPATH" in pinged["envKeysPassed"]
    assert KERNEL_ROOT not in json.dumps(pinged["envKeysPassed"])


# --------------------------------------------------------------------------- #
# 批次五十四（AG-03）：探针别再自己把证据链掐断
# --------------------------------------------------------------------------- #


def test_the_probe_approves_its_own_ping_so_the_tool_really_runs() -> None:
    """先问权限再调工具的那类 agent 上，``--mcp-ping`` 仍要走到 nonce。

    2026-09-22 真机上这一趟是这么断的：agent 发了 ``session/request_permission``，
    **探针自己**回了取消（stderr 里那句 `Denied by user (*)` 是探针说的），工具没
    跑完，nonce 回不来，``mcp_via_session_new`` 只剩半条证据。
    ``--mcp-ping`` 的整个目的就是让那一次工具真的跑完，所以它对**自己这一次
    ping** 自动批准——并且把「批了哪一次」记进输出，免得下一个读报告的人把
    「探针替我批的」当成「用户批的」。
    """
    report = _run("mcp-ping-permission")
    assert report["sessionNew"]["outcome"] == "ok"
    # agent 确实问过一次权限（不然这条用例什么都没测到）。
    assert "session/request_permission" in report["agentRequests"]
    approvals = report["autoApprovals"]
    assert len(approvals) == 1, approvals
    assert approvals[0]["tool"] == TOOL_NAME
    # 放行项是 agent 自己报的那几个里挑的，不是探针编的。
    assert approvals[0]["optionId"] in {"allow", "allow_always"}
    assert TOOL_NAME in json.dumps(approvals[0]["requestVerbatim"], ensure_ascii=False)
    # 批准之后两条判据都成立——工具真的跑完了。
    verdict = report["mcpPing"]
    assert verdict["autoApproveTool"] == TOOL_NAME
    assert verdict["verdict"] == "verified_true"
    assert verdict["toolCallSeen"] is True and verdict["nonceEchoed"] is True


def test_only_the_ping_is_auto_approved_everything_else_is_still_refused() -> None:
    """放行的是**那一个工具名**，不是「所有权限请求」。

    这条守的是安全边界没被放松：同一趟里 agent 为**别的**工具要权限，探针照旧
    回取消，``autoApprovals`` 里一条都不多。
    """
    report = _run("permission")
    assert "session/request_permission" in report["agentRequests"]
    assert report["autoApprovals"] == []
    # 那个工具因此没跑成——agent 自己在更新流里说「denied」。
    blob = json.dumps(report.get("toolUpdateContents"), ensure_ascii=False)
    assert "denied" in blob


def test_two_consecutive_tool_update_contents_make_it_into_the_report() -> None:
    """``tool_update_cumulative`` 那一位只能从**连续两条** content 里读出来。

    2026-09-22 那一趟就是因为脚本没留这两条，这一位取不到（留在 declared，没人
    敢猜）。现在留前两条原文，判「全量还是增量」的人自己看得见。
    """
    report = _run("tool-lifecycle", mcp_ping=False, prompt=True)
    contents = report["toolUpdateContents"]
    assert len(contents) == 2, contents
    first, second = (
        json.dumps(row["content"], ensure_ascii=False) for row in contents
    )
    # 这台假 agent 默认发全量：第二帧是「到目前为止的全部内容」。
    assert "read" in first and "read a file" in second
