"""``mcp`` 能力 → ACP ``mcpServers`` 的翻译规则（批次四十三第 2 件）。

这份测试守三件事，第三件是红线：

1. 两种行粒度（整键表 / 逐条 connection）都翻得出来；
2. 翻不了的条目不静默消失——留一条 warning（D-03）；
3. **任何输出里都不许出现环境变量的值**，除了 ``env[].value`` / ``headers[].value``
   本身那一格。warning 里只许出现名字。
"""

from __future__ import annotations

import json

import pytest

from app.capabilities.models import (
    EffectiveCapabilities,
    EffectiveCapability,
    ProjectCapability,
)
from app.capabilities.resolver import (
    CapabilityResolutionRequest,
    TreeCapabilityResolver,
)
from drivers.acp.mcp_projection import project_mcp_servers

PROJECT = "project:demo"
SECRET = "s3cr3t-not-in-any-log"


def entry(capability_id: str, value: object, *, capability_type: str = "mcp"):
    return EffectiveCapability(
        capability_type=capability_type,
        capability_id=capability_id,
        config={"value": value},
        source_project_id=PROJECT,
        inherited=False,
    )


def effective(*entries: EffectiveCapability) -> EffectiveCapabilities:
    return EffectiveCapabilities(project_id=PROJECT, entries=tuple(entries))


# --------------------------------------------------------------------------- #
# 两种行粒度
# --------------------------------------------------------------------------- #


def test_whole_key_row_becomes_one_server_per_name() -> None:
    """整键行：``{"mcp_servers": {名字: {...}}}``（导入器落库的那种）。"""
    projection = project_mcp_servers(
        effective(
            entry(
                "mcp_servers",
                {
                    "files": {"command": "node", "args": ["server.js"]},
                    "git": {"command": "git-mcp"},
                },
            )
        )
    )
    assert projection.names == ("files", "git")
    assert projection.servers[0] == {
        "name": "files",
        "command": "node",
        "args": ["server.js"],
        "env": [],
    }
    assert not projection.warnings


def test_per_connection_row_uses_the_capability_id_as_the_name() -> None:
    """逐条行：``("mcp", "github")``（v1.0 §5.2 的同 ID connection）。"""
    projection = project_mcp_servers(
        effective(entry("github", {"command": "github-mcp", "args": []}))
    )
    assert projection.names == ("github",)
    assert projection.servers[0]["command"] == "github-mcp"


def test_config_without_the_value_wrapper_also_works() -> None:
    """AD-42 的 ``{"value": …}`` 包装是惯例，不是唯一形状。"""
    raw = EffectiveCapability(
        capability_type="mcp",
        capability_id="bare",
        config={"command": "x"},
        source_project_id=PROJECT,
        inherited=False,
    )
    assert project_mcp_servers(effective(raw)).names == ("bare",)


def test_other_capability_types_are_not_touched() -> None:
    projection = project_mcp_servers(
        effective(
            entry("skill-a", {"command": "nope"}, capability_type="skills"),
            entry("ok", {"command": "yes"}),
        )
    )
    assert projection.names == ("ok",)


# --------------------------------------------------------------------------- #
# 远端形态
# --------------------------------------------------------------------------- #


def test_http_and_sse_keep_the_protocol_field_names() -> None:
    projection = project_mcp_servers(
        effective(
            entry("remote", {"type": "http", "url": "https://example.invalid/mcp"}),
            entry("streamy", {"type": "sse", "url": "https://example.invalid/sse"}),
        )
    )
    assert projection.servers[0] == {
        "type": "http",
        "name": "remote",
        "url": "https://example.invalid/mcp",
    }
    assert projection.servers[1]["type"] == "sse"


def test_a_url_without_a_type_defaults_to_http_quietly() -> None:
    projection = project_mcp_servers(effective(entry("r", {"url": "https://x.invalid"})))
    assert projection.servers[0]["type"] == "http"
    assert not projection.warnings


def test_an_unknown_transport_falls_back_to_http_and_says_so() -> None:
    projection = project_mcp_servers(
        effective(entry("r", {"type": "websocket", "url": "https://x.invalid"}))
    )
    assert projection.servers[0]["type"] == "http"
    assert any("websocket" in w for w in projection.warnings)


# --------------------------------------------------------------------------- #
# env：只从进程环境按名字取
# --------------------------------------------------------------------------- #


def test_env_names_resolve_from_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv("KAUS_TEST_TOKEN", SECRET)
    projection = project_mcp_servers(
        effective(entry("s", {"command": "x", "env": ["KAUS_TEST_TOKEN"]}))
    )
    assert projection.servers[0]["env"] == [
        {"name": "KAUS_TEST_TOKEN", "value": SECRET}
    ]
    assert not projection.warnings


def test_a_mapping_env_reads_only_the_keys(monkeypatch) -> None:
    """配置里那一侧的「值」一个字都不读——就算有人在里面塞了明文。"""
    monkeypatch.setenv("KAUS_TEST_TOKEN", SECRET)
    projection = project_mcp_servers(
        effective(
            entry(
                "s",
                {"command": "x", "env": {"KAUS_TEST_TOKEN": "plaintext-in-config"}},
            )
        )
    )
    assert projection.servers[0]["env"] == [
        {"name": "KAUS_TEST_TOKEN", "value": SECRET}
    ]
    assert "plaintext-in-config" not in json.dumps(projection.servers)


def test_env_keys_is_accepted_as_a_spelling_of_env(monkeypatch) -> None:
    monkeypatch.setenv("KAUS_TEST_TOKEN", SECRET)
    projection = project_mcp_servers(
        effective(entry("s", {"command": "x", "env_keys": ["KAUS_TEST_TOKEN"]}))
    )
    assert projection.servers[0]["env"][0]["name"] == "KAUS_TEST_TOKEN"


def test_a_credential_ref_spelling_yields_the_variable_name(monkeypatch) -> None:
    monkeypatch.setenv("KAUS_TEST_TOKEN", SECRET)
    projection = project_mcp_servers(
        effective(
            entry("s", {"command": "x", "env": ["credential-store:KAUS_TEST_TOKEN"]})
        )
    )
    assert projection.servers[0]["env"][0]["name"] == "KAUS_TEST_TOKEN"


def test_a_missing_variable_is_omitted_and_warned_by_name(monkeypatch) -> None:
    monkeypatch.delenv("KAUS_TEST_MISSING", raising=False)
    projection = project_mcp_servers(
        effective(entry("s", {"command": "x", "env": ["KAUS_TEST_MISSING"]}))
    )
    assert projection.servers[0]["env"] == []
    assert len(projection.warnings) == 1
    assert "KAUS_TEST_MISSING" in projection.warnings[0]


def test_headers_resolve_by_variable_name_too(monkeypatch) -> None:
    monkeypatch.setenv("KAUS_TEST_TOKEN", SECRET)
    projection = project_mcp_servers(
        effective(
            entry(
                "r",
                {
                    "type": "http",
                    "url": "https://x.invalid",
                    "headers": {"Authorization": "KAUS_TEST_TOKEN"},
                },
            )
        )
    )
    assert projection.servers[0]["headers"] == [
        {"name": "Authorization", "value": SECRET}
    ]


def test_a_missing_header_variable_omits_the_header(monkeypatch) -> None:
    monkeypatch.delenv("KAUS_TEST_MISSING", raising=False)
    projection = project_mcp_servers(
        effective(
            entry(
                "r",
                {
                    "url": "https://x.invalid",
                    "headers": {"Authorization": "KAUS_TEST_MISSING"},
                },
            )
        )
    )
    assert "headers" not in projection.servers[0]
    assert any("KAUS_TEST_MISSING" in w for w in projection.warnings)


# --------------------------------------------------------------------------- #
# 红线：warning 里只许有名字
# --------------------------------------------------------------------------- #


def test_no_warning_ever_carries_a_value(monkeypatch) -> None:
    """变量**名**进 warning 是对的（那正是要说的事）；**值**一个字都不许进。

    这里的 ``SECRET`` 只出现在「值」那一侧：进程环境里的值、配置里被误写成
    明文的值、以及一个形状根本不对的头取值。三条路各走一遍，warning 里都不该
    有它。
    """
    monkeypatch.setenv("KAUS_TEST_TOKEN", SECRET)
    monkeypatch.delenv("KAUS_TEST_MISSING", raising=False)
    projection = project_mcp_servers(
        effective(
            entry("resolved", {"command": "x", "env": ["KAUS_TEST_TOKEN"]}),
            entry("absent", {"command": "x", "env": {"KAUS_TEST_MISSING": SECRET}}),
            entry("nocmd", {"env": ["KAUS_TEST_TOKEN"], "args": []}),
            entry("r", {"url": "https://x.invalid", "headers": {"A": {"v": SECRET}}}),
        )
    )
    blob = "\n".join(projection.warnings)
    assert SECRET not in blob
    assert blob, "翻不了的条目必须留下话（D-03：不静默丢弃）"


# --------------------------------------------------------------------------- #
# 跳过与去重
# --------------------------------------------------------------------------- #


def test_a_server_with_neither_command_nor_url_is_skipped_loudly() -> None:
    projection = project_mcp_servers(effective(entry("ghost", {"args": ["x"]})))
    assert projection.servers == ()
    assert any("ghost" in w for w in projection.warnings)


def test_an_empty_closing_value_projects_nothing_and_says_nothing() -> None:
    """AD-45 的关闭值形状（空表）：没什么可送，也没什么可抱怨。"""
    projection = project_mcp_servers(effective(entry("mcp_servers", {})))
    assert projection.servers == ()
    assert projection.warnings == ()


def test_the_same_name_twice_keeps_the_last_one_at_the_first_position() -> None:
    projection = project_mcp_servers(
        effective(
            entry("a", {"command": "first"}),
            entry("z", {"command": "other"}),
            entry("dup", {"name": "a", "command": "second"}),
        )
    )
    assert projection.names == ("a", "z")
    assert projection.servers[0]["command"] == "second"


# --------------------------------------------------------------------------- #
# block：Resolver 已经摘掉了，这里断言一次口径
# --------------------------------------------------------------------------- #


def test_a_blocked_connection_never_reaches_the_wire() -> None:
    """AD-45：祖先挂的 MCP 被子项目 block 掉之后，引擎一个字都收不到。"""
    root, leaf = "project:root", "project:leaf"
    assignments = {
        root: [
            ProjectCapability.create(
                project_id=root,
                capability_type="mcp",
                capability_id="secretive",
                config={"value": {"command": "nope"}},
            ),
            ProjectCapability.create(
                project_id=root,
                capability_type="mcp",
                capability_id="allowed",
                config={"value": {"command": "yes"}},
            ),
        ],
        leaf: [
            ProjectCapability.create(
                project_id=leaf,
                capability_type="mcp",
                capability_id="secretive",
                assignment_mode="block",
            )
        ],
    }
    resolved = TreeCapabilityResolver().resolve(
        CapabilityResolutionRequest(ancestry=(root, leaf)), assignments
    )
    assert resolved.is_blocked("mcp", "secretive")
    projection = project_mcp_servers(resolved)
    assert projection.names == ("allowed",)
    assert "secretive" not in json.dumps(projection.servers)


@pytest.mark.parametrize("value", [None, "a string", 42, ["a", "list"]])
def test_a_config_that_is_not_a_mapping_is_skipped_loudly(value: object) -> None:
    projection = project_mcp_servers(effective(entry("weird", value)))
    assert projection.servers == ()
    assert projection.warnings
