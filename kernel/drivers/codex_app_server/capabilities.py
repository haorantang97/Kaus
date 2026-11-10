"""Declared :class:`BackendCapabilities` of the app-server transport.

Every value below is ``declared`` (read from the protocol source, not measured on a
real binary; the fake-server contract run upgrades the observable rows to
``bench``). Protocol items without a Kaus expression are ``unsupported`` rather
than left blank (N §13.1).
"""

from __future__ import annotations

from runtime.capability_matrix import (
    AuthCapabilities,
    BackendCapabilities,
    CardCapabilities,
    ExternalCliCapabilities,
    ModelCapabilities,
    SessionCapabilities,
    SupportLevel,
    ToolCardCapabilities,
    declare,
)

#: Project capabilities that reach the engine through ``thread/start`` /
#: ``thread/resume`` parameters (``developerInstructions``, ``config.mcp_servers``).
CAPABILITY_PROJECTION = {
    "mcp": SupportLevel.ADAPTED,
    "instructions": SupportLevel.ADAPTED,
    "skills": SupportLevel.UNSUPPORTED,
    "plugins": SupportLevel.UNSUPPORTED,
}

TOKEN_USAGE_NOTE = "thread/tokenUsage/updated 带 total/last 的 token 明细与模型上下文窗口。"
RESUME_NOTE = "冷续接：每条会话一个 codex app-server 进程，thread/resume 读同一份 $CODEX_HOME/sessions 记录。"
INTERRUPT_NOTE = "turn/interrupt 后以 turn/completed(status=interrupted) 收尾；粒度未在真机实测。"
LIST_NOTE = "thread/list 可列全部本地线程，但本驱动只用自己的账本核对，未接发现。"
HISTORY_NOTE = "原生公开历史由 drivers/codex/native_history.py 读 rollout 文件，完整度按每次读取结果报告。"


def declared_capabilities(*, native_history: bool, external_cli: bool = True) -> BackendCapabilities:
    return BackendCapabilities(
        structured_events=declare("supported"),
        sessions=SessionCapabilities(
            list=declare("own_process", note=LIST_NOTE),
            create=declare("supported"),
            resume=declare("cold", note=RESUME_NOTE),
            history=declare("supported", note=HISTORY_NOTE) if native_history else declare("unsupported"),
            branch=declare("unsupported", note="thread/fork 存在，本驱动未接。"),
        ),
        card=CardCapabilities(
            streaming=declare("supported"),
            tools=ToolCardCapabilities(calls=declare("supported"), output=declare("supported")),
            terminal=declare("unsupported", note="命令输出以工具卡增量呈现，不开终端卡。"),
            file_changes=declare("supported"),
            artifacts=declare("unsupported"),
            plan=declare("supported"),
            reasoning=declare("supported", note="只有 reasoning.delta；协议没有思考状态事件。"),
            permissions=declare("protocol"),
            questions=declare("supported", note="item/tool/requestUserInput。"),
            authentication=declare("unsupported", note="登录在 codex CLI 里完成；不弹认证卡。"),
            usage=declare("supported", note=TOKEN_USAGE_NOTE),
            interrupt=declare("immediate", note=INTERRUPT_NOTE),
            attachments=declare("images"),
        ),
        external_cli=(ExternalCliCapabilities(supported=declare("supported"), resume=declare("supported"))
                      if external_cli else
                      ExternalCliCapabilities(supported=declare("unsupported"), resume=declare("unsupported"))),
        models=ModelCapabilities(
            mode="constrained",
            reasoning=declare("supported"),
            providers=declare("unsupported"),
            conversation_scoped=declare("supported", note="turn/start 每轮带 model 与 effort。"),
        ),
        auth=AuthCapabilities(model="own-auth", state_reporting=declare("supported", note="account/read。")),
        capability_projection=dict(CAPABILITY_PROJECTION),
    )


__all__ = ["CAPABILITY_PROJECTION", "declared_capabilities"]
