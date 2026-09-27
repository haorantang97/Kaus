"""通用审批档 ↔ Hermes ``approvals.mode`` 的**映射表（数据）**。

为什么住在 Driver 目录，且和 :mod:`drivers.hermes.delegation_map` 并排
--------------------------------------------------------------------
同一条理由：表里是 Hermes 的私有取值，公共层一个字都不能出现（N §3 /
工作区规则 6）。和委派映射表一样，这张表被两个方向共用——投影器把通用值写成
引擎值，:mod:`drivers.hermes.engine_settings` 把引擎值读成通用值——一张表，
才不会出现「读进来是 A、写回去是 B」。

取证
----
- 配置键路径 ``approvals.mode``、取值集合 ``manual | smart | off``：本仓库
  ``server.py`` 的 ``_APPROVALS_MODES`` 与 ``/api/agent/{name}/levers`` 长期在
  真机上读写这一段（``test_regressions.py`` 断言
  ``_read_config(...)["approvals"]["mode"] == "smart"``），是生产在用的键名；
- ``manual`` 是 Hermes 的默认档（``server.py`` 在写 ``manual`` 时直接删掉
  ``approvals`` 段，靠的就是这个事实）；
- 语义与文案按 **AD-106**：``manual`` 每次询问 / ``smart`` 自动放行（引擎自己
  判断，只拦真危险的）/ ``off`` **全部放行**——任务书原写「全部拒绝」，与 ``off``
  的实际语义相反，AD-106 已纠正。CLI 侧的 ``--yolo``（"Bypass all dangerous
  command approvals"，见 ``docs/probes/2026-09-02-run2-nonlive.md``）是同一件事的
  另一个入口，可作旁证。

**没有第四档。** 通用值只有 :data:`drivers.base.APPROVAL_MODES` 的三个；引擎将来
多出取值时，在本表里加一行，别的地方一行都不用改。

.. deprecated:: 批次三十七（AD-163）

   **公共取值 ``deny`` 会在下一个允许破坏 wire 的批次里改名为 ``bypass``。**

   它现在的语义是「全部放行」（映射到 ``approvals.mode=off``），而 ``deny`` 这个词
   在任何人的第一直觉里都是「拒绝」——一个**语义反转的安全取值**。中文界面已经用
   「全部放行」把它说清楚了，但公共 API、未来的适配器和维护这段代码的人读到的是
   `deny` 本身。外部评审（`docs/quality/external-review-2026-09-07.md` 产品判断 #5）
   独立地把它点了出来，那说明它确实会被读反。

   **本批不改**：`deny` 已经在公共 wire 上（`PATCH /api/bindings/{id}` 的
   `approvalMode`、能力矩阵、投影报告），改名是破坏性变更，不该混在一批安全修复里
   悄悄发出去。改名批次要做的事写在 AD-163 里：新值 `bypass` + 一个显式的兼容层
   （读入两个值都认、写出只用新值）+ 一次迁移校验。

   **在那之前，任何按名字推断语义的代码都是错的**——判据只有本表里这一行，
   以及 :data:`app.capabilities.monotonic.SAFETY_FIELDS` 里 `approval_mode` 的强度
   顺序（`ask` > `auto` > `deny`，最松的是 `deny`；AD-150 把它写成数据正是因为
   这个名字读不出方向）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping

from drivers.base import APPROVAL_MODES, ApprovalMode

#: ``config.yaml`` 里这一段的键路径（段名, 子键名）。
CONFIG_SECTION: Final[str] = "approvals"
CONFIG_KEY: Final[str] = "mode"

#: 完整键路径，投影的 ``target_ref`` 与报错文案共用。
CONFIG_PATH: Final[str] = f"{CONFIG_SECTION}.{CONFIG_KEY}"

#: Hermes 不写这一段时的档位（= 引擎默认）。
NATIVE_DEFAULT: Final[str] = "manual"


@dataclass(frozen=True)
class ApprovalModeEntry:
    """一档审批：通用取值 ↔ 引擎取值 + 这一档到底放行什么。"""

    generic: ApprovalMode
    native: str
    #: 给人看的一句话（进 README / 投影的 ``detail``，不进对话页——AD-71）。
    meaning: str


#: **映射表**。顺序 = :data:`drivers.base.APPROVAL_MODES` 的顺序（有测试守着）。
APPROVAL_MODE_MAP: Final[tuple[ApprovalModeEntry, ...]] = (
    ApprovalModeEntry("ask", "manual", "每次询问：命中危险判定就停下来问人（引擎默认档）"),
    ApprovalModeEntry("auto", "smart", "自动放行：引擎自己判断，只拦真危险的"),
    ApprovalModeEntry("deny", "off", "全部放行：不再询问（AD-106：off 是放行，不是拒绝）"),
)

_TO_NATIVE: Mapping[str, str] = {e.generic: e.native for e in APPROVAL_MODE_MAP}
_TO_GENERIC: Mapping[str, ApprovalMode] = {e.native: e.generic for e in APPROVAL_MODE_MAP}


def to_native(generic: str | None) -> str | None:
    """通用取值 → ``approvals.mode``。表外的值返回 ``None``（不猜、不落盘）。"""
    return _TO_NATIVE.get(generic) if generic is not None else None


def to_generic(native: str | None) -> ApprovalMode | None:
    """``approvals.mode`` → 通用取值。表外的值返回 ``None``（引擎加了新档时不编造）。"""
    if native is None:
        return None
    return _TO_GENERIC.get(str(native).strip().lower())


def mapping_table_json() -> list[dict[str, str]]:
    """映射表导出成可打印/可断言的 JSON（报告与 README 用）。"""
    return [
        {"generic": e.generic, "native": f"{CONFIG_PATH}={e.native}", "meaning": e.meaning}
        for e in APPROVAL_MODE_MAP
    ]


__all__ = [
    "APPROVAL_MODES",
    "APPROVAL_MODE_MAP",
    "CONFIG_KEY",
    "CONFIG_PATH",
    "CONFIG_SECTION",
    "NATIVE_DEFAULT",
    "ApprovalModeEntry",
    "mapping_table_json",
    "to_generic",
    "to_native",
]
