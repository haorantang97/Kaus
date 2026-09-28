"""Mock Backend 的能力声明夹具（批次六：枚举轴 + unknown + verification）。

为什么单独一个模块
------------------
Mock 的职责不只是「跑通契约测试」，还要**替 UI 覆盖每一种取值**：枚举轴的每个
值都对应一种界面形态（``interrupt=tool_boundary`` 的按钮文案与 ``immediate``
不同、``sessions.list=own_process`` 时不出现「从引擎导入会话」入口……），前端拿
不到真实引擎的时候就得靠这里的预设做开发与截图。

:data:`CAPABILITY_PRESETS` 的并集覆盖 :data:`~runtime.capability_matrix.ENUM_AXES`
里每根轴的每个取值，:func:`enum_value_coverage` 把这件事变成可断言的数据
（见 ``drivers/contract_tests/test_mock_driver_contract.py``）。
"""

from __future__ import annotations

from typing import Mapping

from runtime.capability_matrix import (
    ENUM_AXES,
    AuthCapabilities,
    BackendCapabilities,
    CardCapabilities,
    ExternalCliCapabilities,
    ModelCapabilities,
    SessionCapabilities,
    SupportLevel,
    ToolCardCapabilities,
    capability_state_at,
    declare,
)

MOCK_USAGE_NOTE: str = (
    "用量只报上下文占用，不报 token 计费用量（夹具刻意留的一项 detail 层备注）。"
)

MOCK_BRANCH_NOTE: str = (
    "分支（fork）没实测过，夹具把它留成 unknown，供契约测试验「跳过但计数」那条路。"
)


#: 全能力预设：枚举轴取各自最强的一档，工具卡两个子项都有。
DEFAULT_CAPABILITIES = BackendCapabilities(
    structured_events=declare("supported", verification="bench"),
    sessions=SessionCapabilities(
        list=declare("all", verification="bench"),
        create=declare("supported", verification="bench"),
        resume=declare("warm", verification="bench"),
        history=declare("supported", verification="bench"),
        # 夹具刻意留一项 unknown：契约测试要验「unknown 跳过但计数」。
        branch=declare("unknown", note=MOCK_BRANCH_NOTE),
    ),
    card=CardCapabilities(
        streaming=True,
        tools=ToolCardCapabilities(calls=True, output=True),
        terminal=True,
        file_changes=True,
        artifacts=True,
        plan=True,
        reasoning=True,
        permissions=declare("protocol", verification="bench"),
        questions=True,
        # AD-08：authentication.resolved 进公共 union 后，Mock 也把认证卡的
        # 完整闭环声明为支持，契约测试才跑得到这条正向分支。
        authentication=True,
        usage=declare("supported", note=MOCK_USAGE_NOTE),
        interrupt=declare("immediate", verification="bench"),
        # 附件：图片与任意文件都收（不读内容，回复里点名收到了哪些）。
        attachments=declare("files", verification="bench"),
    ),
    external_cli=ExternalCliCapabilities(supported=True, resume=True),
    models=ModelCapabilities(
        mode="open",
        reasoning=True,
        providers=True,
        # 批次十六第 4 件：Mock 的每一轮都按 Conversation 快照挑模型，
        # 因此会话级切换在这一档是**支持**的（真引擎里这根轴各不相同）。
        conversation_scoped=declare("supported", verification="bench"),
    ),
    # AD-93 / 批次十九：假引擎报得出登录态（固定的假值），供前端做三态渲染。
    auth=AuthCapabilities(
        model="managed-credential",
        state_reporting=declare("supported", verification="bench"),
    ),
    capability_projection={
        "skills": SupportLevel.NATIVE,
        "mcp": SupportLevel.ADAPTED,
        "instructions": SupportLevel.ADAPTED,
        # 显式不支持：N §13.1 要求不支持项返回明确状态，而不是缺省/空对象。
        "plugins": SupportLevel.UNSUPPORTED,
    },
)


#: 一个能力被大幅削减的 Backend 声明，用来验证契约套件的**降级分支**：
#: 不支持列 Session、不支持 Permission/Question/Interrupt、不支持站外 CLI、
#: 模型是 fixed。N §13.1 要求这些「不支持」必须是显式状态。
LEAN_CAPABILITIES = BackendCapabilities(
    structured_events=True,
    sessions=SessionCapabilities(
        list="none", create=True, resume="none", history=False, branch=False
    ),
    card=CardCapabilities(
        streaming=True,
        tools=ToolCardCapabilities(calls=True, output=False),
        permissions="none",
        interrupt="none",
    ),
    external_cli=ExternalCliCapabilities(supported=False, resume=False),
    models=ModelCapabilities(mode="fixed"),
    # 「引擎自己有登录流程，但站内查不到状态」——AD-71 的静默隐藏在登录这一行上
    # 的样子：两行都不渲染。这是真实存在的处境（own-auth 的引擎多半如此），
    # 所以夹具里必须有一档长这样。
    auth=AuthCapabilities(model="own-auth", state_reporting="none"),
    capability_projection={"skills": SupportLevel.ADAPTED},
)


#: 冷续接 + 只看得见本进程的会话 + 审批只有镜像 + 只能在工具边界中断。
#: 这一档是真实 Agent 里最常见的形状，UI 开发主要照着它做。
COLD_REATTACH_CAPABILITIES = BackendCapabilities(
    structured_events=True,
    sessions=SessionCapabilities(
        list=declare("own_process", verification="live"),
        create=True,
        resume=declare("cold", verification="live"),
        history=False,
        branch=False,
    ),
    card=CardCapabilities(
        streaming=True,
        tools=ToolCardCapabilities(calls=True, output=False),
        reasoning=True,
        permissions=declare("mirror", verification="live"),
        interrupt=declare("tool_boundary", verification="live"),
        usage=False,
    ),
    external_cli=ExternalCliCapabilities(supported=False, resume=False),
    models=ModelCapabilities(mode="constrained"),
    capability_projection={"mcp": SupportLevel.ADAPTED},
)


#: 「什么都还没探测过」：每题都是 unknown（缺省即如此）。UI 要能把这一档渲染成
#: 「未知」，而不是把它误当成「确认不支持」。
UNPROBED_CAPABILITIES = BackendCapabilities()


#: 「自称有审批，但连走的是哪条路都还没确认」——枚举轴上的 ``unverified``。
#: 它比 ``unknown`` 多一点信息（知道它自称有），所以是独立取值而不是未知。
UNVERIFIED_APPROVAL_CAPABILITIES = BackendCapabilities(
    structured_events=True,
    sessions=SessionCapabilities(create=True),
    card=CardCapabilities(
        streaming=True,
        tools=ToolCardCapabilities(calls=True, output=False),
        permissions="unverified",
    ),
    models=ModelCapabilities(mode="fixed"),
)


#: 「点了停止，引擎没认」——批次十六第 2 件的真机形态：声明能停，实测没确认。
#: 按钮照常显示（请求发得出去），只是不承诺立刻停。
UNVERIFIED_INTERRUPT_CAPABILITIES = BackendCapabilities(
    structured_events=True,
    sessions=SessionCapabilities(create=True),
    card=CardCapabilities(
        streaming=True,
        tools=ToolCardCapabilities(calls=True, output=False),
        interrupt=declare("unverified", verification="live"),
    ),
    models=ModelCapabilities(mode="fixed"),
)


#: 供 UI 与契约测试遍历的预设表。并集覆盖每根枚举轴的每个取值。
CAPABILITY_PRESETS: Mapping[str, BackendCapabilities] = {
    "default": DEFAULT_CAPABILITIES,
    "lean": LEAN_CAPABILITIES,
    "cold-reattach": COLD_REATTACH_CAPABILITIES,
    "unprobed": UNPROBED_CAPABILITIES,
    "unverified-approval": UNVERIFIED_APPROVAL_CAPABILITIES,
    "unverified-interrupt": UNVERIFIED_INTERRUPT_CAPABILITIES,
}


def enum_value_coverage() -> Mapping[str, frozenset[str]]:
    """每根枚举轴在预设表里出现过的取值集合。"""
    coverage: dict[str, set[str]] = {axis: set() for axis in ENUM_AXES}
    for capabilities in CAPABILITY_PRESETS.values():
        for axis in ENUM_AXES:
            coverage[axis].add(capability_state_at(capabilities, axis).value)
    return {axis: frozenset(values) for axis, values in coverage.items()}


def missing_enum_values() -> Mapping[str, tuple[str, ...]]:
    """还没被任何预设覆盖到的枚举取值（正常应为空）。"""
    coverage = enum_value_coverage()
    return {
        axis: tuple(v for v in values if v not in coverage[axis])
        for axis, values in ENUM_AXES.items()
        if any(v not in coverage[axis] for v in values)
    }


__all__ = [
    "CAPABILITY_PRESETS",
    "COLD_REATTACH_CAPABILITIES",
    "DEFAULT_CAPABILITIES",
    "LEAN_CAPABILITIES",
    "MOCK_BRANCH_NOTE",
    "MOCK_USAGE_NOTE",
    "UNPROBED_CAPABILITIES",
    "UNVERIFIED_APPROVAL_CAPABILITIES",
    "UNVERIFIED_INTERRUPT_CAPABILITIES",
    "enum_value_coverage",
    "missing_enum_values",
]
