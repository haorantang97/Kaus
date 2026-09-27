"""AgentEventEnvelope v1.1 冻结测试（2026-09-02）。

这个文件只做一件事：**让 union 动不了**。

冻结清单 :data:`FROZEN_V1_1_EVENT_TYPES` 是抄在测试里的第三份副本
（第一份是 ``runtime/event_envelope.py`` 的实现，第二份是
``runtime/ENVELOPE_CHANGELOG.md`` 的事件全表）。三份必须**逐字相等**——
增一条、删一条、改个名、换个顺序，都会红。

为什么要三份而不是一份
----------------------
- 只有实现：改实现就没人拦得住，冻结形同虚设；
- 实现 + 测试：能拦住误改，但文档会悄悄过期，接 Driver 的人读到的是旧表；
- 加上文档：改 union 的人必须同时改三处，那就不再是「顺手改一下」，
  而是一次需要解释理由的动作——这正是冻结想要的摩擦。

红了怎么办
----------
先回答一个问题：这是**新增事件**还是**新增可选字段**？

- 新增事件 → 按 ``ENVELOPE_CHANGELOG.md`` 的变更规则第 1 条，它属于 v1.2，
  在 v1.1 里先走 ``extension.event``。不要改这份清单。
- 新增可选字段 → 允许（规则 2），但事件全表没变，这个测试本来就不该红；
  红了说明顺手动了别的东西。
"""

from __future__ import annotations

import re
from pathlib import Path

from runtime.event_envelope import AGENT_EVENT_TYPES, SCHEMA_VERSION

#: 冻结日期。Envelope v1.1 自此定稿（AD-08 + AD-27 补完即冻结）。
FROZEN_ON = "2026-09-02"

#: 冻结版本号。v1.1 之后只允许加可选字段；新增事件必须进 v1.2。
FROZEN_SCHEMA_VERSION = "1.1"

#: **冻结清单**：v1.1 公共事件 union 的全部 30 条，顺序即 union 顺序。
#: 这份清单不随实现变化——它是实现必须满足的约束，不是实现的镜像。
FROZEN_V1_1_EVENT_TYPES: tuple[str, ...] = (
    "session.created",
    "session.resumed",
    "session.state",
    "run.started",
    "run.completed",
    "run.interrupted",
    "run.failed",
    "message.started",
    "message.delta",
    "message.completed",
    "reasoning.delta",
    "reasoning.status",
    "plan.updated",
    "tool.started",
    "tool.updated",
    "tool.completed",
    "terminal.started",
    "terminal.updated",
    "terminal.completed",
    "file.changed",
    "artifact.created",
    "permission.requested",
    "permission.resolved",
    "question.requested",
    "question.resolved",
    "authentication.requested",
    "authentication.resolved",
    "usage.updated",
    "diagnostic.notice",
    "extension.event",
)

CHANGELOG = Path(__file__).resolve().parent.parent / "ENVELOPE_CHANGELOG.md"


def _changelog_event_table() -> tuple[tuple[str, ...], tuple[int, ...]]:
    """从 ``ENVELOPE_CHANGELOG.md`` 的「公共事件全表」里抽出事件名，按表中顺序。

    只认形如 ``| 12 | `x.y` | ...`` 的行：序号 + 反引号包起来的事件名。
    这样文档里其它地方提到事件名（例如「不在 v1.1 内」的表格）不会被误收。
    """
    pattern = re.compile(r"^\|\s*(\d+)\s*\|\s*`([a-z_]+\.[a-z_]+)`\s*\|")
    rows: list[tuple[int, str]] = []
    for line in CHANGELOG.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if match is not None:
            rows.append((int(match.group(1)), match.group(2)))
    return tuple(name for _, name in rows), tuple(number for number, _ in rows)


def test_event_union_equals_the_frozen_manifest_verbatim() -> None:
    """冻结的核心断言：实现与清单**逐字相等**，任何增删即失败。"""
    assert AGENT_EVENT_TYPES == FROZEN_V1_1_EVENT_TYPES, (
        "公共事件 union 与 v1.1 冻结清单不一致。\n"
        f"多出来：{sorted(set(AGENT_EVENT_TYPES) - set(FROZEN_V1_1_EVENT_TYPES))}\n"
        f"少掉了：{sorted(set(FROZEN_V1_1_EVENT_TYPES) - set(AGENT_EVENT_TYPES))}\n"
        "新增事件属于 v1.2（见 runtime/ENVELOPE_CHANGELOG.md 变更规则第 1 条），"
        "在 v1.1 里先走 extension.event。"
    )


def test_frozen_manifest_has_no_duplicates_and_a_fixed_size() -> None:
    """清单本身要自洽：30 条、不重复。"""
    assert len(FROZEN_V1_1_EVENT_TYPES) == 30
    assert len(set(FROZEN_V1_1_EVENT_TYPES)) == 30


def test_schema_version_is_frozen_at_1_1() -> None:
    """版本号也在冻结范围内：动它就是宣布 v1.2。"""
    assert SCHEMA_VERSION == FROZEN_SCHEMA_VERSION == "1.1"


def test_changelog_event_table_matches_the_frozen_manifest() -> None:
    """文档的事件全表 = 冻结清单，且序号连续——文档不许悄悄过期。"""
    names, numbers = _changelog_event_table()
    assert names == FROZEN_V1_1_EVENT_TYPES, (
        "ENVELOPE_CHANGELOG.md 的事件全表与冻结清单不一致；"
        "改 union 必须同时改实现、清单与文档三处。"
    )
    assert numbers == tuple(range(1, len(FROZEN_V1_1_EVENT_TYPES) + 1))


def test_changelog_records_the_freeze_date_and_rules() -> None:
    """冻结日期与变更规则必须白纸黑字写在文档里，不能只活在提交信息里。"""
    text = CHANGELOG.read_text(encoding="utf-8")
    assert FROZEN_ON in text
    assert "FROZEN" in text
    assert "v1.2" in text, "变更规则必须指明新增事件的去处"
    assert "extension.event" in text


def test_retired_and_deferred_events_stay_out_of_the_union() -> None:
    """作废的与延后的都不得混进冻结的 union。

    ``run.spawned`` 已被信封头 ``parentRunId`` 取代（AD-08 / 裁决表 #5）；
    ``context.compacted`` / ``input.consumed`` / ``tool_group`` 是 v1.2 候选，
    v1.1 里只能走 ``extension.event``（AD-27）。
    """
    for retired in (
        "run.spawned",
        "context.compacted",
        "input.consumed",
        "tool_group",
    ):
        assert retired not in AGENT_EVENT_TYPES
        assert retired not in FROZEN_V1_1_EVENT_TYPES
