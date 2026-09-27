"""ACP 预设目录：**一张只读表**，把「常用的那几个桌面 Agent」接进来只用写一行配置。

这个文件是本包里**唯一**允许出现具体产品名的地方，理由与它的形状是同一件事
（AD-151）：

- 目录本身就是「一张产品清单」——它的每一行说的就是「这个产品叫什么、怎么拉起来」，
  把产品名从这里删掉，这张表就没有内容了；
- 但 **Driver 一行产品名都不许有**。Driver 只读 :class:`AgentQuirks` 的字段做分支，
  永远不写 ``if agent_name == ...``。新增一个引擎 = 这张表里加一行，Driver 一个字
  不改。``drivers/acp/tests/test_purity.py`` 用机械断言守住这条分工：本文件在
  产品名扫描里被豁免，其余每个文件都不得出现任何预设 id 的字面量。

三层优先级（AD-151）
--------------------
``initialize`` 返回的 ``agentCapabilities`` **优先于**预设，预设优先于协议默认值。
预设填的是「``initialize`` 没说的那些位」，不是「我们认为它应该是什么」——实测
永远压过声明，这是 N §13.1 的直接后果。因此本表的取值一律只到 ``declared`` 取证
等级；契约跑一遍能把摸得着的那几题升到 ``bench``。

不确定就填保守值
----------------
拿不准的位一律填「不支持」：多声明一项不存在的能力，界面上会多出一个点了没反应
的按钮；少声明一项其实存在的能力，界面上只是少一个入口，而 ``initialize`` 一旦
声明了它就会自动补回来。两种错误的代价不对等。

``resume_argv_template``
------------------------
给 Terminal Launcher 的「在 CLI 里打开」用：``{native_session_id}`` 是唯一占位符。
填 ``None`` 表示**不确定这个 CLI 怎么续接**——那时 ``launch.external`` 保持
``unsupported``，界面上直接没有这个入口（AD-71 缺能力静默隐藏），而不是给一条
猜出来的命令让用户在终端里吃一个报错。

``auth_method_ids``
-------------------
``initialize`` 返回的 ``authMethods[].id`` 抄一遍（批次三十二取证，AD-156）。
**只有 id**——名字、说明、``vars`` 里的变量名一律不进来：这一列的用途是让界面能
说一句「先在终端登录：``chatgpt`` / ``openai-api-key`` …」，不是复述一份登录向导。
它也**不是**能力位：Driver 不读它做任何分支，``initialize`` 每次连上都会带回权威
的那一份，这里存的只是「没连上时也能告诉用户去登什么」。空元组 = 那次取证没拿到
（进程没起来），或者这个适配器明说它没有 ACP 内的登录方法（``authMethods: []``）。

``login_command``
-----------------
「没登录的时候，用户该在终端敲什么」——**只有这一句话的用途**。它不是能力位，
Driver 不读它做任何分支；它出现在两个地方：目录探测撞上「需要登录」时的诊断
（「需要先在终端登录：…」），与引擎卡登录行的下一步。填 ``None`` 表示**我们不
确定这家 CLI 的登录命令**，那时界面只说「先在终端把这台引擎登录好」，不给一条
猜出来的命令让用户吃报错（与 ``resume_argv_template`` 同一条纪律，AD-71）。

**仪表盘永远不代跑它。** 这一列是给用户看的文本，不是给我们执行的 argv：ACP 的
``authenticate`` 我们一次都不调（AD-93 own-auth），登录发生在用户自己的终端里。

``env_keys_hint``
-----------------
**只是提示**，只有变量名，且**不自动放行**任何东西：子进程环境仍然只透传
``backends[].env_keys`` 白名单里显式列过的名字（AD-10 / AD-48）。这一列存在的
唯一目的，是让文档与界面能说一句「这个引擎通常要 ``XXX``」。

``verified_bits``（批次三十四 / AD-158）
----------------------------------------
「这一位是真机跑出来的，还是照公开文档填的」。批次二十五承认过整张表只到
``declared``；批次三十二在无凭据容器里把握手那几位钉死；本批测试员在云机上对着
一条 OpenAI 兼容中继把八家（外加三家新的）真的跑了一遍回合、工具、取消、续接、
换模式、换模型。取证到哪一位，哪一位就进这个集合，能力矩阵上对应的那一格因此
标 ``live`` 而不是 ``declared``。

它**不是**能力位——Driver 一个字都不读它做分支。它回答的是「这句话有多硬」，
不是「这台引擎能不能干这件事」。空集合表示这一行整张表还是声明级，不是「实测
为假」；两者混在一起，界面上就会把「没测过」画成「测过、不支持」。

``workspace``（批次四十四 / AD-167）
------------------------------------
这一家在**工作目录**里读哪个文件（:class:`WorkspaceConventions`）。协议里一个字
都没有，所以它只能来自各家的公开约定或真机探测；目前**一行都没有真机依据**，
``instructions_file`` 因此登记在 :data:`UNMEASURED_BITS` 里，没有任何一行把它写进
``verified_bits``。填 ``None`` = 不知道 → 该引擎的 ``instructions`` 一律按不支持
处理，不猜文件名（理由与 ``resume_argv_template`` 逐字相同）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import sys
from typing import Literal, Mapping

#: 所有预设的登录模型都是 ``own-auth``（AD-93）：ACP 的凭据不经 Kaus 托管，
#: 登录发生在各家 CLI 自己的流程里，仪表盘不代登录、也查不到状态。
AUTH_MODEL: str = "own-auth"

#: ``resume_argv_template`` 里唯一允许的占位符。
SESSION_ID_PLACEHOLDER: str = "{native_session_id}"


@dataclass(frozen=True)
class AgentQuirks:
    """一个 ACP 实现在协议留白处的选择（AD-151 的「怪癖表」）。

    每一位都对应 Driver 里一处**真实存在的分支**。加一位之前先问：Driver 会不会
    因为它走不同的路？不会就不该加——这张表不是给人看的备忘录，是给代码读的开关。
    """

    #: AD-49：``tool_call_update.content`` 是全量快照还是增量。协议没规定，实测
    #: 到的都是全量，因此默认 ``True``。某个 agent 实测发增量时在这里登记一行，
    #: 公共信封（``ToolUpdated.cumulative``）不改。
    tool_update_cumulative: bool = True
    #: 认不认 ``session/resume``（实测：跨进程续接靠它）。
    supports_session_resume: bool = True
    #: 认不认 ``session/load``。实测存在「参数校验永远失败」的实现，所以默认不认。
    supports_session_load: bool = False
    #: 认不认 ``session/set_mode``（用它切审批档）。
    supports_set_mode: bool = False
    #: 认不认 ``session/set_model``（或等价的 ``_meta`` 扩展）。不认时能力表的
    #: ``models.conversation_scoped`` 报 ``unsupported``——会话级换模型这条路不通。
    supports_set_model: bool = False
    #: 发不发 ``agent_thought_chunk``。不发时 ``card.reasoning`` 报 ``unsupported``。
    thought_chunks: bool = True
    #: 要不要客户端替它读写文件（``fs/read_text_file`` / ``fs/write_text_file``）。
    #: 只有它为真、且 Binding 给了 workspaceRoot 时，客户端才声明 ``fs`` 能力
    #: （AD-152）。
    needs_client_fs: bool = False

    # ------------------------------------------------------------------ #
    # 批次三十四：真机跑出来的三件事，原来的七位表达不了（AD-158）
    # ------------------------------------------------------------------ #

    #: ``session/set_mode`` 换的到底是**什么档**（AD-158）。
    #:
    #: 协议只说「会话有若干 mode，可以切」，没说这些 mode 是什么意思。真机上有
    #: 两种完全不同的东西共用这一个方法：一种是**审批档**（default / acceptEdits /
    #: bypassPermissions / plan…），另一种是**思考档**（off / minimal / low /
    #: medium / high / xhigh）。把 Binding 的 ``approval_mode`` 映射到后者，等于
    #: 用户按下「自动批准」换来的是「多想一会儿」——一次静默的语义错位。
    #:
    #: - ``approval``：这一位是审批档，映射 Binding 的 ``approval_mode``；
    #: - ``thought_level``：这一位是思考档，改映射 Binding 的
    #:   ``reasoning_effort``，**一个字都不碰审批**；
    #: - ``none``：没有可切的档（``supports_set_mode`` 为假时的伴生值）。
    mode_semantics: Literal["approval", "thought_level", "none"] = "approval"
    #: 会话级换模型走哪条路（AD-158）。
    #:
    #: - ``set_model``：``session/set_model``（协议里那一处）；
    #: - ``config_option``：``session/set_config_option``，``optionId = "model"``，
    #:   值是 ``configOptions[category=model].options[].value`` 的**逐字原文**
    #:   （真机上见过普通 id，也见过 ``["provider","model"]`` 这种 JSON 元组字符串
    #:   ——我们不解析它，原样送回去）；
    #: - ``none``：这台引擎没有会话级换模型这条路。
    #:
    #: 不变式：``supports_set_model`` ⇔ ``model_switch == "set_model"``。旧的那一位
    #: 仍是能力表 ``models.conversation_scoped`` 的判据（它问的是「协议里那个方法认
    #: 不认」），新的这一位才是 Driver 下发时走哪条路的开关。
    model_switch: Literal["set_model", "config_option", "none"] = "none"
    #: 换模型时 modelId 的形状（AD-158）。
    #:
    #: - ``plain``：目录里报什么就发什么；
    #: - ``effort_suffix``：必须写成 ``modelId[effort]``（真机上发裸 id 回
    #:   ``-32603 Unsupported format``）。目录里读到带后缀的 id 时，**显示**去掉
    #:   后缀、**发送**保留原文——两者是两件事，混在一起就会发出一个引擎不认的 id。
    model_id_format: Literal["plain", "effort_suffix"] = "plain"
    #: ``session/resume`` 之前要不要先 ``session/close``（AD-158）。
    #:
    #: 真机上有一家在同进程里对**还活着**的会话 resume 会回 ``-32602 already
    #: active``，先 ``session/close`` 一次就好（换一个新进程也好）。协议没规定这个
    #: 先后关系，所以它只能是目录里的一位。引擎不认 ``session/close``（``-32601``）
    #: 时照常往下走——那说明它压根不需要这一步。
    resume_requires_close: bool = False
    #: 两种恢复方法都被声明时优先 load；协议 UUID 不一定是原生 resume key。
    prefer_session_load: bool = False

    # ------------------------------------------------------------------ #
    # 批次四十三：项目能力到底送不送得到引擎
    # ------------------------------------------------------------------ #

    #: ``session/new`` 的 ``mcpServers`` 到底管不管用（批次四十三）。
    #:
    #: 协议里这个参数是**必填**的（实测：缺它一律 ``-32602``），所以「发得出去」
    #: 早就有真机依据；但「发进去之后 agent 真的把那台 MCP 挂上了」是另一件事，
    #: 协议不保证，也没有任何响应字段能读出来。默认 ``True`` = 相信协议；
    #: 实测某一家收下不挂时，在 :attr:`AcpPreset.known_bad` 里按版本把它按住
    #: （AD-164 的机制照旧，只能关不能开）。
    #:
    #: Driver 读这一位做分支：为假时**不往 ``session/new`` 里塞 MCP**，
    #: 而不是塞了之后假装成功——后者会让界面上的「已投射」变成一句谎话。
    #:
    #: 12 个预设全部停在 ``declared``（``verified_bits`` 里都不含它）：要升
    #: ``live`` 得靠 ``probe_adapter --mcp-ping`` 在真机上跑一次。
    mcp_via_session_new: bool = True

    def to_wire(self) -> dict[str, object]:
        """只读投影，供 ``GET /api/backends/{id}`` 输出。"""
        return {
            "toolUpdateCumulative": self.tool_update_cumulative,
            "supportsSessionResume": self.supports_session_resume,
            "supportsSessionLoad": self.supports_session_load,
            "supportsSetMode": self.supports_set_mode,
            "supportsSetModel": self.supports_set_model,
            "preferSessionLoad": self.prefer_session_load,
            "thoughtChunks": self.thought_chunks,
            "needsClientFs": self.needs_client_fs,
            "modeSemantics": self.mode_semantics,
            "modelSwitch": self.model_switch,
            "modelIdFormat": self.model_id_format,
            "resumeRequiresClose": self.resume_requires_close,
            "mcpViaSessionNew": self.mcp_via_session_new,
        }


@dataclass(frozen=True)
class WorkspaceConventions:
    """这个 agent 在**工作目录**里读哪些文件（批次四十四 / AD-167）。

    与 :class:`AgentQuirks` 分开，是因为它回答的不是同一种问题：怪癖表说的是
    「这台引擎在协议留白处怎么选」，这里说的是「它在磁盘上认哪个文件名」。
    前者由 ``initialize`` 可能纠正，后者协议里一个字都没有——只能来自各家的
    公开约定，或真机上探一次。

    ``instructions_file``
    ---------------------
    项目指令写进工作目录里的哪个文件（相对工作目录的文件名）。

    ``None`` = **我们不知道这家的约定**。那时 ``materialize`` 把 ``instructions``
    放进 ``unsupported``（``reason = no_convention``），界面上如实说一句「该引擎
    未声明项目指令文件约定」——而不是猜一个文件名写进用户的工作目录（AD-71 /
    AD-167：不装能做）。猜错的代价不是「少一个功能」，是在别人的仓库里留下一个
    没人读的文件，并且界面上说「已应用」。
    """

    instructions_file: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {"instructionsFile": self.instructions_file}


#: 怪癖位的字段名 → wire 键名。取证等级（``AcpPreset.verified_bits``）用字段名
#: 记录，上 wire 时换成同一份键名——两处说的是同一位，不许各起各的名字。
#:
#: 批次四十四起这张表里也有**不属于 ``AgentQuirks`` 的位**（``instructions_file``
#: 在 :class:`WorkspaceConventions` 上）：``verified_bits`` 问的是「这句话有多硬」，
#: 而「这家读哪个文件」同样有硬有软，用另一套取证记法只会变成两本账。
#: :meth:`AcpPreset.forced_off_bits` 因此额外过一道「它真的是一位布尔怪癖吗」。
QUIRK_BITS: Mapping[str, str] = {
    "tool_update_cumulative": "toolUpdateCumulative",
    "supports_session_resume": "supportsSessionResume",
    "supports_session_load": "supportsSessionLoad",
    "supports_set_mode": "supportsSetMode",
    "supports_set_model": "supportsSetModel",
    "thought_chunks": "thoughtChunks",
    "needs_client_fs": "needsClientFs",
    # 批次四十三：能不能把项目 MCP 随 session/new 送进去。全部预设先停在
    # declared——没有任何一行把它写进 verified_bits。
    "mcp_via_session_new": "mcpViaSessionNew",
    # 批次四十四：这家在工作目录里读哪个指令文件。目录里填了值的那几行是照各家
    # **公开文档**填的，仓库里的取证记录（docs/forensics/）一个字都没有涉及它，
    # 所以 12 行一律不进 verified_bits。
    "instructions_file": "instructionsFile",
}

#: **目录里一行都没有真机依据**的那几位（批次四十三）。
#:
#: 加一位新怪癖时，写 ``verified_bits=frozenset(QUIRK_BITS)`` 的那几行会**静默**
#: 把新位也算成「测过了」——能力矩阵上凭空多一格 ``live``，界面上一个字不变。
#: 所以新位先登记在这里，:data:`ALL_MEASURED_BITS` 把它减掉；哪天真机探过了，
#: 从这里删掉那一位，对应的行就自动升上去。
UNMEASURED_BITS: frozenset[str] = frozenset(
    {"mcp_via_session_new", "instructions_file"}
)

#: 「到目前为止，真机确实逐位跑过的那一整套」。全量取证的行用它，而不是
#: ``frozenset(QUIRK_BITS)``——理由见 :data:`UNMEASURED_BITS`。
ALL_MEASURED_BITS: frozenset[str] = frozenset(QUIRK_BITS) - UNMEASURED_BITS


@dataclass(frozen=True)
class AcpPreset:
    """目录里的一行。"""

    id: str
    label: str
    #: 怎么把它以 ACP 模式拉起来（argv，不做 shell 拼接）。
    command: tuple[str, ...]
    #: 安装提示与静态检测元数据；不由 Driver 执行安装命令。
    install_command: str | None = None
    setup_url: str | None = None
    executable: str | None = None
    #: 已核实的扩展方法名；通用驱动按语义键处理，不按产品名分支。
    client_methods: Mapping[str, str] = field(default_factory=dict)
    elicitation_forms: bool = False
    #: 原生权限档有歧义时采用精确映射；非空表不再猜测未列出的通用档。
    approval_mode_ids: Mapping[str, str] = field(default_factory=dict)
    #: 与工作模式分列的权限配置项；只认显式声明，不靠 id 猜权限。
    approval_option_id: str | None = None
    #: 部分实现将思考项归入 model，明确声明它自己的 id，避免把档位当模型。
    thought_option_id: str | None = None
    #: 仅创建本地记录/静态目录的桥不能据此声称模型已登录。
    session_creation_proves_auth: bool = True
    #: 官方运行时建议的非敏感默认值；用户现有环境优先，不改全局环境。
    process_env: Mapping[str, str] = field(default_factory=dict)
    #: 「在 CLI 里打开」的续接命令模板；``None`` = 不确定，不显示这个入口。
    resume_argv_template: tuple[str, ...] | None = None
    group_isolation_adapter: str | None = None
    # Shared by ordinary conversations and coordinator executions. Only file
    # metadata is watched; credentials remain owned by the engine.
    config_root_env: str | None = None
    config_root_default: str | None = None
    config_watch_files: tuple[str, ...] = ()
    config_xdg_dir: str | None = None
    workspace_config_dir: str | None = None
    auth_model: str = AUTH_MODEL
    #: 只有变量名的提示，**不自动放行**（AD-10 / AD-48）。
    env_keys_hint: tuple[str, ...] = ()
    #: ``initialize`` 里 ``authMethods[].id`` 的取证记录（AD-156）。只有 id。
    auth_method_ids: tuple[str, ...] = ()
    #: 「先在终端运行这一句」。``None`` = 不确定这家 CLI 怎么登录（AD-157）。
    login_command: str | None = None
    quirks: AgentQuirks = field(default_factory=AgentQuirks)
    #: 它在**工作目录**里读哪些文件（批次四十四）。缺省全是 ``None`` = 不知道。
    workspace: WorkspaceConventions = field(default_factory=WorkspaceConventions)
    #: 这一行里**哪几位是真机跑出来的**（AD-158）。取值是 :data:`QUIRK_BITS` 的
    #: 字段名。
    #:
    #: 它不是能力位，Driver 一个字都不读它做分支；它只回答一个问题：能力矩阵上
    #: 这一格该标 ``live`` 还是 ``declared``。空集合 = 这一行整张表都还是声明级
    #: （AD-151 承认的那种「按公开文档与保守原则填的」），不是「实测为假」。
    verified_bits: frozenset[str] = frozenset()
    #: **AD-164：已复现的版本缺陷。** ``适配器版本前缀 -> 必须强制关掉的怪癖位``。
    #:
    #: 这是 AD-151「``initialize`` 声明 > 预设」那条优先级的**唯一例外**，而且它
    #: 只往一个方向开：**只能把位关掉，不能打开**。理由见 AD-164——预设写的是
    #: 「我们猜它行不行」，可以被引擎自己的话覆盖；这张表写的是「这个版本上我们
    #: **实测它不行**」，那不是猜测，被一句自报覆盖掉的后果是用户按下续接得到一个
    #: ``-32603``。
    #:
    #: 匹配用**前缀**（``"0.8"`` 命中 ``0.8.0`` / ``0.8.3``）：一个已复现的实现
    #: 缺陷通常横跨整条补丁线，逐个版本号登记只会漏。版本读自 ``agentInfo.version``；
    #: **读不到版本就一位都不关**——那时我们并不知道装的是哪一版，凭空关一位与凭空
    #: 开一位一样是编造。
    known_bad: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: 给人看的一句话（登录方式、装没装、哪些位没实测过）。
    notes: tuple[str, ...] = ()

    def verified(self, bit: str) -> bool:
        """这一位有没有真机依据（字段名，见 :data:`QUIRK_BITS`）。"""
        return bit in self.verified_bits

    def forced_off_bits(self, version: str | None) -> tuple[str, ...]:
        """这个版本上必须强制关掉的怪癖位（AD-164）。

        版本为 ``None``（agent 没报 ``agentInfo.version``）时返回空——不知道装的
        是哪一版，就不替它做决定（N §13.1）。
        """
        if not version:
            return ()
        forced: list[str] = []
        for prefix, bits in self.known_bad.items():
            if version.startswith(prefix):
                # 只认**布尔怪癖**：QUIRK_BITS 里也有非布尔的取证位（批次四十四的
                # `instructions_file`），把它放进 `replace(quirks, …)` 会直接炸，
                # 而且「强制关掉一个文件名」本来就没有意义。
                forced.extend(
                    bit
                    for bit in bits
                    if bit in QUIRK_BITS and bit in AgentQuirks.__dataclass_fields__
                )
        return tuple(sorted(set(forced)))

    def resume_argv(self, native_session_id: str) -> tuple[str, ...] | None:
        """把模板填成一条真正的 argv。没有模板就是 ``None``。"""
        if self.resume_argv_template is None:
            return None
        return tuple(
            part.replace(SESSION_ID_PLACEHOLDER, native_session_id)
            for part in self.resume_argv_template
        )

    @property
    def supports_external_cli(self) -> bool:
        return self.resume_argv_template is not None

    def to_wire(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "label": self.label,
            "authModel": self.auth_model,
            "supportsExternalCli": self.supports_external_cli,
        }
        if self.env_keys_hint:
            payload["envKeysHint"] = list(self.env_keys_hint)
        if self.auth_method_ids:
            payload["authMethodIds"] = list(self.auth_method_ids)
        if self.login_command:
            payload["loginCommand"] = self.login_command
        if self.install_command:
            payload["installCommand"] = self.install_command
        if self.setup_url:
            payload["setupUrl"] = self.setup_url
        if self.executable:
            payload["executable"] = self.executable
        if self.workspace.instructions_file:
            # 不知道约定时整个键不出现（AD-71：说不出的就不说），而不是回 null
            # 让前端为「有这个键但它是空的」再分一条支。
            payload["workspace"] = self.workspace.to_wire()
        if self.verified_bits:
            # 只出**已取证**的那几位（空时整个键不出现，AD-71）。
            payload["verifiedBits"] = sorted(
                QUIRK_BITS[bit] for bit in self.verified_bits if bit in QUIRK_BITS
            )
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


def _preset(preset: AcpPreset) -> tuple[str, AcpPreset]:
    return preset.id, preset


#: 目录本体。key = 预设 id，就是 ``backends[].preset`` 里写的那个字符串。
PRESETS: Mapping[str, AcpPreset] = dict(
    (
        _preset(
            AcpPreset(
                id="claude-code",
                executable="claude",
                setup_url="https://github.com/agentclientprotocol/claude-agent-acp",
                group_isolation_adapter="drivers.group_execution.claude",
                config_root_env="CLAUDE_CONFIG_DIR",
                config_root_default="~/.claude",
                config_watch_files=("settings.json", "settings.local.json"),
                label="Claude Code",
                command=("npx", "@agentclientprotocol/claude-agent-acp@latest"),
                resume_argv_template=("claude", "--resume", SESSION_ID_PLACEHOLDER),
                # 它没有子命令式的登录入口：直接跑 `claude` 走交互式登录。
                login_command="claude",
                # 只有名字，**不自动放行**：中继路线要用这两个变量时，仍须在
                # backends[].env_keys 里显式列出来才透传得进子进程（AD-10 / AD-48）。
                env_keys_hint=("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"),
                workspace=WorkspaceConventions(instructions_file="CLAUDE.md"),
                quirks=AgentQuirks(
                    # 真机上第二个**发增量**的引擎（AD-160）：与 openclaw 同类，
                    # AD-49 那条「实测到的都是全量」的默认值又多了一个反例。
                    tool_update_cumulative=False,
                    thought_chunks=True,
                    supports_session_resume=True,
                    supports_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="approval",
                    needs_client_fs=True,
                    supports_set_model=False,
                    model_switch="config_option",
                ),
                auth_method_ids=(),
                verified_bits=ALL_MEASURED_BITS,
                notes=(
                    "工作目录指令文件按它的**公开约定**填 CLAUDE.md（仓库里的取证记录没有涉及这一项，所以它不进 verified_bits，界面上仍是 declared）。",
                    "SDK 适配器；登录走它自己的 CLI（先在终端登好，仪表盘不代登录）。",
                    "审批档经 session/set_mode 下发。",
                    "取证 0.75.1（AD-156）：authMethods 是空数组——它不提供 ACP 内的"
                    "登录方法，登录态整个来自 CLI 的家目录，所以「去登录」只能指向终端。",
                    "取证 0.75.1：session/set_mode 认（回 {}），session/set_model 回 "
                    "-32601；换模型走 configOptions（id=model），因此 model_switch="
                    "config_option。",
                    "取证 0.75.1（AD-160，中继真机）：七位怪癖全部实测——工具更新是"
                    "**增量**（tool_update_cumulative=false）、思考流有（一个工具回合"
                    " 34 段）、modes 是真审批档"
                    "（default/acceptEdits/plan/auto/bypassPermissions）、"
                    "session/cancel 约 0.5s 回 cancelled、resume 与 load 都通。",
                    "取证 0.75.1：configOptions 里模型那一项的取值是 Claude 自己的"
                    "**档位 id**（default/opus/sonnet/haiku），不是裸模型名——目录里"
                    "原样列这几个 id 并配上它们的显示名，一个字都不解析、不翻译。",
                    "中继路线（AD-160）：把 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN"
                    "（env 或它自己的 settings，**只有名字**）指到一条 Anthropic 兼容"
                    "端点，这台引擎就整体换了 provider；档位 id 不变，背后是谁由那条"
                    "路线决定。值一律不进仓库、不上 wire。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="codex",
                executable="codex",
                setup_url="https://github.com/agentclientprotocol/codex-acp",
                config_root_env="CODEX_HOME",
                config_root_default="~/.codex",
                config_watch_files=("config.toml", "auth.json"),
                group_isolation_adapter="drivers.group_execution.codex",
                label="Codex",
                command=("npx", "@agentclientprotocol/codex-acp@latest"),
                resume_argv_template=("codex", "resume", SESSION_ID_PLACEHOLDER),
                login_command="codex login",
                env_keys_hint=("OPENAI_API_KEY", "CODEX_API_KEY"),
                auth_method_ids=("chatgpt", "codex-api-key", "openai-api-key"),
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=True,
                    supports_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="approval",
                    supports_set_model=True,
                    model_switch="set_model",
                    model_id_format="effort_suffix",
                ),
                verified_bits=ALL_MEASURED_BITS,
                notes=(
                    "工作目录指令文件按它的**公开约定**填 AGENTS.md（同上，未经真机取证）。",
                    "订阅登录或 API key 二选一；用 key 时只写变量名到 env_keys。",
                    "取证 0.16.0（AD-156）：未登录时 session/new 回 -32000 "
                    "«Authentication required»，不带 data——「去登录」只能靠 authMethods。",
                    "取证 1.10.0（AD-158，中继真机）：包名换成 "
                    "@agentclientprotocol/codex-acp（@zed-industries/… 已 deprecated）；"
                    "七位怪癖全部实测：工具更新全量单发、思考流有、set_mode 认"
                    "（read-only / agent / agent-full-access）、resume 与 load 都通、"
                    "cancel 约 0.5s 回 cancelled。",
                    "取证 1.10.0：session/set_model 的 modelId 必须写成 modelId[effort]"
                    "，裸 id 回 -32603 «Unsupported format»——因此 model_id_format="
                    "effort_suffix。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="opencode",
                process_env={"NODE_USE_SYSTEM_CA": "1"},
                executable="opencode",
                setup_url="https://opencode.ai/docs/acp/",
                install_command="npm install -g opencode-ai",
                config_root_default="~/.config/opencode",
                config_xdg_dir="opencode",
                workspace_config_dir=".",
                config_watch_files=("opencode.json", "opencode.jsonc"),
                label="OpenCode",
                command=("npx", "opencode-ai@latest", "acp"),
                resume_argv_template=("opencode", "-s", SESSION_ID_PLACEHOLDER),
                # authMethods 的说明原话就是这一句（AD-156 取证）。
                login_command="opencode auth login",
                auth_method_ids=("opencode-login",),
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=True,
                    supports_session_load=True,
                    supports_set_mode=False,
                    mode_semantics="none",
                    supports_set_model=True,
                    model_switch="set_model",
                ),
                verified_bits=frozenset(
                    {
                        "tool_update_cumulative",
                        "supports_session_resume",
                        "supports_session_load",
                        "supports_set_mode",
                        "supports_set_model",
                        "thought_chunks",
                    }
                ),
                notes=(
                    "工作目录指令文件按它的**公开约定**填 AGENTS.md；它也读 CLAUDE.md，我们只写一份——两份受管块等于同一段话在同一个目录里有两个出处。",
                    "登录走它自己的 CLI（authMethods 的说明原话：`opencode auth login`）。",
                    "取证 1.18.29（AD-156）：**未登录也能开会话**——session/new 直接成功，"
                    "登录与否要到 session/prompt 才见分晓。",
                    "取证 1.18.29（AD-158，中继真机）：**session/new 一个 modes 都不报**，"
                    "set_mode 回 -32602 Invalid params——批次三十二那次「回 {}」是拿"
                    "当前值空跑的假阳性，本行 supports_set_mode 因此改回 false。",
                    "取证 1.18.29：set_model 认，但 modelId 是 `provider/model` 形状；"
                    "resume 与 load 两条 RPC 都真的通。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="gemini",
                executable="gemini",
                setup_url="https://github.com/google-gemini/gemini-cli",
                install_command="npm install -g @google/gemini-cli",
                config_root_default="~/.gemini",
                config_watch_files=("settings.json",),
                label="Gemini CLI",
                command=("npx", "@google/gemini-cli@latest", "--experimental-acp"),
                resume_argv_template=None,
                env_keys_hint=("GEMINI_API_KEY",),
                # 交互式登录：直接跑它本体，按提示选一种 authMethod。
                login_command="gemini",
                auth_method_ids=(
                    "oauth-personal",
                    "gemini-api-key",
                    "vertex-ai",
                    "gateway",
                ),
                workspace=WorkspaceConventions(instructions_file="GEMINI.md"),
                quirks=AgentQuirks(
                    supports_session_resume=False,
                    needs_client_fs=True,
                    mode_semantics="none",
                ),
                verified_bits=frozenset(),
                notes=(
                    "工作目录指令文件按它的**公开约定**填 GEMINI.md（未经真机取证）。",
                    "ACP 面带 --experimental 前缀，形状可能随版本变。",
                    "续接命令未实测，因此不提供「在 CLI 里打开」入口。",
                    "取证 0.58.0（AD-156）：agentCapabilities **整个没有 sessionCapabilities**"
                    "（只有 loadSession=true）。这一位此前填 True 是没有依据的猜测，"
                    "而 initialize 没说的位由预设兜底、不会被自动纠正，故按 AD-151 的"
                    "保守规则改成 False：续接这条路要么走 session/load，要么先别显示。",
                    "取证 0.58.0：无 key 时 session/new 回 -32000 «Gemini API key is "
                    "missing or not configured.»，不带 data。",
                    "中继真机（AD-158）按计划跳过它，所以怪癖七位一位都没升 live。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="antigravity",
                executable="agy_acp_server.par",
                setup_url="https://github.com/agentclientprotocol/registry/tree/main/antigravity-acp",
                label="Google Antigravity",
                # 官方清单登记的是一个**平台二进制**，不是 npm 包：解压出来的那个
                # 文件叫 agy_acp_server.par，但它没有固定安装位置，所以预设只给
                # 文件名（放进 PATH，或在 backends[].command 里覆盖成绝对路径）。
                # ⚠️ 同包里还有第二个文件（见 notes 第一条），少了它起不来。
                command=("agy_acp_server.par",),
                resume_argv_template=None,
                env_keys_hint=(),
                # 它这家的 CLI 登录入口确实是 `agy`，所以这个字段留着；但它
                # **不够**——ACP 服务端另有一份 OAuth（真机取证，见 notes 第三条）。
                login_command="agy",
                auth_method_ids=(),
                workspace=WorkspaceConventions(instructions_file=None),
                quirks=AgentQuirks(
                    # batch54：2026-09-22 真机取证（agy_acp_server_1.1.1），逐位
                    # 依据见 notes 最后一条。此前整行是照保守档填的猜测。
                    supports_session_resume=True,
                    supports_session_load=True,
                    supports_set_mode=True,
                    supports_set_model=True,
                    model_switch="set_model",
                    mode_semantics="approval",
                    thought_chunks=True,
                    # 取证只到「它没向我们要过文件」，而探针从没给它派过文件任务。
                    # False 是更谦逊的那一档（True 会让我们多声明一项能力并去当它的
                    # 文件代理），所以填 False 但**不进** verified_bits。
                    needs_client_fs=False,
                ),
                verified_bits=frozenset(
                    {
                        "supports_session_resume",
                        "supports_session_load",
                        "supports_set_mode",
                        "supports_set_model",
                        "thought_chunks",
                    }
                ),
                known_bad={},
                notes=(
                    "官方 ACP 目录清单 v1.1.1（`agentclientprotocol/registry` 仓库的 "
                    "`antigravity-acp/agent.json`，作者 Google LLC）登记的下载地址："
                    "macOS arm64 是 https://dl.google.com/agy-extensions/releases/macos/"
                    "agy-acp-server-agy_acp_server_1.1.1-darwin-arm64.zip，解压后清单里的"
                    " `cmd` 就是 `./agy_acp_server.par`。"
                    "⚠️ **解压出来的不止这一个文件**（batch53 / 真机 MV-04）：同包的 "
                    "`localharness_external` 也要一起装，否则 `session/new` 报 "
                    "«Could not find default localharness binary»。把它和 `.par` 放同一个"
                    "目录（或一起放进 PATH），必要时用 `ANTIGRAVITY_HARNESS_PATH` 指到它"
                    "的绝对路径——真机上就是这么绕过去的。",
                    "Linux x86_64 那条清单在 `cmd` 之后还多一个 `args: [\"--uid=\"]`，"
                    "macOS 那条**没有**——所以预设里**不加**：一个只在单个平台的清单里"
                    "出现、含义我们没取证过的空值参数，替用户加上去与猜一条命令是同一件"
                    "事（AD-71）。要用 Linux 版就在 backends[].command 里自己写全。",
                    "登录：**`agy` 登好了也不算数**（batch53 / 真机 MV-05 勘误）。"
                    "此前这里写的是「先在终端跑一次 `agy`，ACP 服务端复用缓存下来的凭据」"
                    "——那是官方 headless 文档对 `agy -p` 说的话，不是对 ACP 服务端说的。"
                    "真机上 `agy` 已经登录，ACP 服务端**仍然单独弹了一次浏览器授权**："
                    "它用的是**另一份 Google OAuth**。所以首次连接时需要用户当场在浏览器里"
                    "完成那一次授权，没人在场就连不上。"
                    "取证只到「另一份 OAuth、会弹浏览器」这一层，**没有**取证到一条可以"
                    "预先跑的登录命令，所以这里也不写一条（AD-71：不猜）。"
                    "`login_command` 字段仍是 `agy`——它确实是这家的 CLI 登录入口，"
                    "只是不足以满足 ACP 服务端。"
                    "权限配置在它自己的 `~/.gemini/antigravity-cli/settings.json` 里，"
                    "仪表盘既不读也不写。",
                    "许可是 proprietary（条款 https://antigravity.google/terms）。"
                    "**产品方 2026-09-19 裁定**（AD-170）：Kaus 的定位是「启动器」——"
                    "一个 ACP 客户端，与 Zed / JetBrains 同列；接一个由它自己登记进 ACP "
                    "公共目录的服务端不算第三方绕行。条款风险由产品方承担。",
                    "工作目录指令文件**留空**：官方文档没写这家的约定，按 AD-167 / AD-71 "
                    "一律当作不支持，不猜一个文件名写进用户的仓库。",
                    "取证 1.1.1（batch54，2026-09-22 真机，`agy_acp_server_1.1.1`）："
                    "**有依据的六位**——`initialize` 声明 `sessionCapabilities.resume` 与 "
                    "`loadSession: true`（resume / load 两位据此升 live）；"
                    "`session/set_mode`、`session/set_model` 各回一次 ok（result 都是 "
                    "`{}`，只是重设当前值）；第二趟事件流里出现 `agent_thought_chunk`；"
                    "`session/new` 报的 modes 是 `default` / `auto_edit` / `yolo`，描述逐字"
                    "是 Default permission prompt flow / Auto-approve file edit tools / "
                    "Auto-approve all tools —— 是**审批档**，所以 mode_semantics="
                    "`approval`（它不是 QUIRK_BITS 里的一位，记不进 verified_bits，"
                    "依据就在这条 notes 里）。"
                    "**没有依据的两位**——`tool_update_cumulative` 取不到（探针那一趟没"
                    "保留连续两条 `tool_call_update.content`），留在 declared，不猜；"
                    "`needs_client_fs` 填 `False`（客户端 fs 声明 false 也跑通，全程没发过"
                    " `fs/*`）但**不进 verified_bits`**：证据只到「它没向我们要过文件」，"
                    "而探针从没给它派过文件任务——没派任务就不需要文件，不等于给了任务也"
                    "不需要。",
                    "后四位（set_mode / set_model / mode_semantics / needs_client_fs）"
                    "**不会被 `initialize` 纠正**：`capabilities.py` 只用 `initialize` 覆盖 "
                    "`sessionCapabilities.resume` 与 `loadSession`，协议里根本没有声明另外"
                    "那几位的字段。所以填错就是真的行为错——用户给它设的审批档会**静默"
                    "不生效**（它明明有三档可切），会话级换模型会报「不支持」（明明 ok）。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="qwen",
                executable="qwen",
                setup_url="https://github.com/QwenLM/qwen-code",
                install_command="npm install -g @qwen-code/qwen-code",
                label="Qwen Code",
                command=(
                    "npx",
                    "@qwen-code/qwen-code@latest",
                    "--acp",
                    "--experimental-skills",
                ),
                resume_argv_template=None,
                env_keys_hint=("OPENAI_API_KEY",),
                # 同上：交互式登录，没有独立的登录子命令。
                login_command="qwen",
                auth_method_ids=("openai",),
                workspace=WorkspaceConventions(instructions_file="QWEN.md"),
                quirks=AgentQuirks(
                    supports_session_resume=True,
                    supports_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="approval",
                    supports_set_model=True,
                    model_switch="set_model",
                    needs_client_fs=True,
                ),
                verified_bits=frozenset(
                    {
                        "tool_update_cumulative",
                        "supports_session_resume",
                        "supports_session_load",
                        "supports_set_mode",
                        "supports_set_model",
                        "thought_chunks",
                    }
                ),
                notes=(
                    "工作目录指令文件按它的**公开约定**填 QWEN.md（未经真机取证）。",
                    "续接命令未实测，不提供「在 CLI 里打开」入口。",
                    "取证 0.23.0（AD-156）：未登录时 session/new 回 -32000 «Authentication "
                    "required: Use Qwen Code CLI to authenticate first.»，且 **data 里带一份"
                    " authMethods**——七个适配器里只有它把「去登录什么」放进了错误体。",
                    "取证 0.23.0（AD-158，中继真机）：登录后六位全通——工具更新全量、"
                    "思考流有、modes 是真审批档（plan/default/auto-edit/auto/yolo）、"
                    "set_model 换得动、resume 与 load 都 OK。needs_client_fs 那一位仍是"
                    "声明级（探针自己声明了 fs 能力，没有反证）。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="openclaw",
                executable="openclaw",
                login_command="openclaw onboard",
                session_creation_proves_auth=False,
                config_root_default="~/.openclaw",
                config_root_env="OPENCLAW_STATE_DIR",
                config_watch_files=("openclaw.json", "agents/*/agent/models.json"),
                setup_url="https://docs.openclaw.ai/cli/acp",
                label="OpenClaw",
                command=("npx", "openclaw", "acp"),
                resume_argv_template=None,
                auth_method_ids=(),
                quirks=AgentQuirks(
                    # 真机上第一个**发增量**的引擎（AD-158）：一次 tool_call + 两条
                    # 只带新片段的 tool_call_update。AD-49 那条默认值的反例。
                    tool_update_cumulative=False,
                    supports_session_resume=True,
                    supports_session_load=True,
                    prefer_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="thought_level",
                    model_switch="none",
                    mcp_via_session_new=False,
                ),
                verified_bits=frozenset(
                    {
                        "tool_update_cumulative",
                        "supports_set_mode",
                        "supports_set_model",
                    }
                ),
                notes=(
                    "登录走它自己的 CLI。",
                    "Kaus 使用原生接口补齐会话级模型选择；"
                    "仅修改当前会话，已配置的自定义厂家一并读取。",
                    "2026-09-27 本机 2026.9.6 隔离验收：非空 mcpServers 被拒绝；"
                    "跨进程恢复 ACP sessionId 要用 session/load，session/resume 不接受该 UUID。",
                    "取证 2026.6.34（AD-156）：`openclaw acp` **不是自带模型的适配器，"
                    "是一座桥**——它连本机 openclaw 服务端的 WebSocket（默认 "
                    "127.0.0.1:18789）。那个服务端没起来时它一个字都不回 initialize，"
                    "直接 «ACP bridge failed: connect ECONNREFUSED» 退出 1。"
                    "所以配它之前得先让那个服务端跑起来。",
                    "取证 2026.9.2（AD-158，中继真机，网关跑在 127.0.0.1:18789）："
                    "工具更新是**增量**（tool_update_cumulative=false，七行里唯一的"
                    "一个 false）；session/new 报的 modes 是思考档"
                    "（off/minimal/low/medium/high/adaptive），不是审批档，因此 "
                    "mode_semantics=thought_level；ACP 面上没有换模型的入口。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="pi",
                executable="pi-acp",
                setup_url="https://github.com/svkozak/pi-acp",
                install_command="npm install -g @earendil-works/pi-coding-agent pi-acp",
                config_root_default="~/.pi/agent",
                workspace_config_dir=".pi",
                config_watch_files=("settings.json", "models.json"),
                label="Pi",
                command=("pi-acp",),
                resume_argv_template=None,
                quirks=AgentQuirks(
                    supports_session_resume=False,
                    supports_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="thought_level",
                    model_switch="config_option",
                    mcp_via_session_new=False,
                ),
                auth_method_ids=("pi_terminal_login",),
                verified_bits=frozenset(
                    {
                        "supports_session_resume",
                        "supports_session_load",
                        "supports_set_mode",
                        "supports_set_model",
                        "thought_chunks",
                    }
                ),
                notes=(
                    "社区适配器，装了才有；未装时 probe 会如实报 unavailable。",
                    "2026-09-27 上游 README 明确 session/new 的 mcpServers 只保存、"
                    "不接入 Pi；项目 MCP 投射关闭，原生扩展仍由 Pi 管理。",
                    "取证 0.0.33（AD-156）：适配器本身在 npm 上（`pi-acp`），但它只是壳——"
                    "session/new 会去 exec `pi`，没装时回 -32603 «Could not start pi: "
                    "executable not found»（data.code=ENOENT），提示装 "
                    "@earendil-works/pi-coding-agent。两件东西都要装。",
                    "取证 0.0.33：sessionCapabilities 只有 {list, delete}，**没有 resume**"
                    "——本表 supports_session_resume=False 这一位得到证实。",
                    "取证 0.0.33（AD-158，中继真机）：session/resume 回 -32601、"
                    "session/load 真的通；modes 是思考档"
                    "（off/minimal/low/medium/high/xhigh），**不是审批档**，所以 "
                    "mode_semantics=thought_level；set_model 回 -32601，换模型走 "
                    "configOptions；思考流实测有（27 段），此前那个 false 是猜的。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="hermes-acp",
                executable="hermes",
                setup_url="https://github.com/NousResearch/hermes-agent",
                config_root_default="~/.hermes",
                config_root_env="HERMES_HOME",
                config_watch_files=("config.yaml",),
                group_isolation_adapter="drivers.hermes.group_execution",
                label="Hermes (ACP)",
                command=("hermes", "acp"),
                resume_argv_template=None,
                quirks=AgentQuirks(
                    supports_session_resume=True,
                    supports_set_mode=True,
                    supports_set_model=True,
                    mode_semantics="approval",
                    model_switch="set_model",
                ),
                verified_bits=frozenset(
                    {
                        "tool_update_cumulative",
                        "supports_set_mode",
                        "thought_chunks",
                    }
                ),
                notes=(
                    "AD-33：这是它的第二条路；走原生 HTTP 面用 driver=native-http。",
                    "tool_call_update 实测发全量（AD-49 默认值的来源）。",
                    "批次三十二的取证在容器里做，那里没有 hermes 可执行文件，"
                    "所以这一行的 auth_method_ids 仍是空的（未取证，不是「没有」）。",
                    "取证 0.19.0（AD-158，中继真机）：装法是 pip `hermes-agent[acp]`"
                    "（npm 上没有这个包），bin 是 `hermes-acp`，`hermes acp` 与它等价；"
                    "modes 是真审批档（default/accept_edits/dont_ask）；工具更新全量、"
                    "思考流很密（46 段）；模型在 session/new 里报。",
                    "2026-09-26 本机版本已实现 session/set_model，选择包含厂家前缀的模型 id。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="dsh",
                executable="dsh",
                setup_url="https://github.com/deepseek-ai/DeepSeek-Harness",
                config_root_default="~/.dsh",
                config_root_env="DSH_HOME",
                config_watch_files=("config.yaml", "settings.yaml", "profiles/*/cordis.patch.yml"),
                label="DeepSeek Harness (ACP)",
                command=("dsh", "--profile", "acp"),
                resume_argv_template=None,
                # authMethods 是空数组，靠环境变量（AD-158 取证）。
                login_command=None,
                env_keys_hint=("DSH_HOME", "DEEPSEEK_API_KEY"),
                auth_method_ids=(),
                quirks=AgentQuirks(
                    supports_session_resume=True,
                    resume_requires_close=True,
                    supports_session_load=False,
                    supports_set_mode=False,
                    mode_semantics="none",
                    supports_set_model=False,
                    model_switch="config_option",
                ),
                verified_bits=frozenset(
                    {
                        "tool_update_cumulative",
                        "supports_session_resume",
                        "supports_session_load",
                        "supports_set_mode",
                        "supports_set_model",
                    }
                ),
                notes=(
                    "官方 harness 的 ACP 面；authMethods 是空数组，凭据只经环境变量"
                    "（env_keys 里显式列 DEEPSEEK_API_KEY 才透传得进去）。",
                    "取证 0.0.1（AD-158，中继真机）：session/set_mode 与 session/set_model"
                    "都回 -32601；换模型走 session/set_config_option（optionId=model），"
                    "值形如 [\"deepseek-official\",\"deepseek-v4-flash\"] 这种 JSON 元组"
                    "字符串——**原样送回去，不解析**。另有一项 reasoning_effort 的 "
                    "configOption。",
                    "取证 0.0.1：对**还活着**的会话 session/resume 回 -32602 «already "
                    "active»，先 session/close 一次（或换一个新进程）就通；session/load"
                    "回 -32601。resume_requires_close 这一位就是为它加的。",
                    "上游包问题：0.1.2-rc.1 的插件与 0.1.1-rc.2 的 dsh-attachment 有"
                    "依赖歪斜，要把对齐版本嵌套安装才起得来——修法见测试员的 "
                    "docs/quality/walkthrough-round2/RELAY-SETUP.md，不是 Kaus 这边能修的。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="deepseek-acp",
                session_creation_proves_auth=False,
                executable="deepseek-acp",
                setup_url="https://www.npmjs.com/package/deepseek-acp",
                install_command="npm install -g deepseek-acp",
                label="DeepSeek ACP",
                command=("npx", "-y", "deepseek-acp"),
                resume_argv_template=None,
                login_command="deepseek-acp --setup",
                config_root_env="DSH_HOME",
                config_root_default="~/.dsh",
                config_watch_files=("settings.yaml", ".credentials.yaml", ".env"),
                workspace_config_dir=".dsh",
                approval_option_id="sandbox",
                thought_option_id="reasoning",
                approval_mode_ids={"read_only": "read-only", "bypass": "danger-full-access"},
                env_keys_hint=("DEEPSEEK_API_KEY",),
                auth_method_ids=(),
                quirks=AgentQuirks(
                    # 0.9.0 本机真实会话已验证 load/resume；0.8 的已知错误仍按版本关闭。
                    supports_session_resume=True,
                    supports_session_load=True,
                    prefer_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="none",
                    supports_set_model=False,
                    model_switch="config_option",
                ),
                verified_bits=frozenset(
                    {
                        "tool_update_cumulative",
                        "supports_session_resume",
                        "supports_session_load",
                        "supports_set_mode",
                        "supports_set_model",
                        "thought_chunks",
                    }
                ),
                notes=(
                    "社区适配器（editor-facing）；0.9.0 复用原生 DSH_HOME 下的"
                    "设置与凭据，也支持环境变量，Kaus 不读取凭据内容。",
                    "2026-09-27 本机 0.9.0：本地确定性模型完成流式回合，跨进程 load"
                    "恢复首轮上下文；close 后同会话 resume 通过。未用云模型取证。",
                    "default/plan 仅是工作模式；sandbox 为独立文件范围，"
                    "danger-full-access 仍可能触发原生工具审批，不代表全部免审。"
                    "workspace-write 不冒充 ask/auto；reasoning 的 category=model"
                    "通过显式 thought_option_id 分离。",
                    "取证 0.8.0（AD-158，中继真机）：prompt / 工具 / cancel / set_mode"
                    "（default、plan）都通；set_model 回 -32601，换模型走 "
                    "session/set_config_option（optionId=model）。",
                    "取证 0.8.0：sessionCapabilities 里有 resume、loadSession 也报 true，"
                    "**但两条 RPC 在同进程里都回 -32603 Internal error**——该旧版本"
                    "的两项能力继续按 known_bad 关闭。",
                    "AD-164：0.8.x 上这两位由 known_bad 强制关闭，不再被它自己的"
                    "initialize 声明翻回 true；界面上这两个入口因此直接不显示"
                    "（AD-71），而不是点了回一个 -32603。0.9 不受此 0.8 前缀限制。",
                ),
                known_bad={
                    # 取证：`docs/forensics/acp-adapters-2026-09-06.md` + AD-158 的
                    # 中继真机（0.8.0）。前缀写到小版本，补丁线整条命中。
                    "0.8": ("supports_session_resume", "supports_session_load"),
                },
            )
        ),
        _preset(
            AcpPreset(
                id="kilo",
                process_env={"NODE_USE_SYSTEM_CA": "1"},
                executable="kilo",
                setup_url="https://kilo.ai/docs/code-with-ai/platforms/cli",
                install_command="npm install -g @kilocode/cli",
                config_root_default="~/.config/kilo",
                config_xdg_dir="kilo",
                workspace_config_dir=".",
                config_watch_files=("kilo.json", "kilo.jsonc"),
                label="Kilo",
                command=("kilo", "acp"),
                resume_argv_template=None,
                login_command=None,
                env_keys_hint=("DEEPSEEK_API_KEY",),
                auth_method_ids=(),
                quirks=AgentQuirks(
                    supports_session_resume=True,
                    supports_set_mode=False,
                    # configOptions 的 code/ask/debug 是工作模式，通用配置入口下发；
                    # 不翻译成审批权限或推理强度。
                    mode_semantics="none",
                    supports_set_model=False,
                    model_switch="config_option",
                ),
                verified_bits=frozenset(
                    {
                        "tool_update_cumulative",
                        "supports_set_mode",
                        "supports_set_model",
                    }
                ),
                notes=(
                    "工作目录指令文件**留空**：任务书原表按「待探」写了 AGENTS.md，"
                    "但仓库里的取证记录（docs/forensics/acp-adapters-2026-09-06.md）"
                    "与 docs/ops/backends.md 都没有涉及这一项，公开约定我们也没查实。"
                    "猜一个文件名写进用户的仓库，代价是留下一个没人读的文件 + 界面上"
                    "说「已应用」——所以按 AD-167 当作不支持（AD-71 静默隐藏）。",
                    "原生 ACP 面（`kilo acp`），优先于社区的 kilo-acp 壳——后者会拒 "
                    "initialized 通知，且有一次工具回合完全没有工具事件。",
                    "取证 7.5.15（AD-158，中继真机）：session/set_model 是 null，"
                    "模式与模型**都**在 configOptions 里（mode=code/ask/debug…，"
                    "model=provider/模型）；工具更新全量单发。",
                    "工作模式经通用 configOptions 入口下发，独立于权限选择。"
                    "supports_set_mode=false 只表示不调用旧 session/set_mode；"
                    "2026-09-26 已通过协议模拟验收，本机尚未安装 Kilo 做真机复测。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="goose",
                label="Goose",
                command=("goose", "acp"),
                executable="goose",
                setup_url="https://goose-docs.ai/docs/gdk/acp/",
                login_command="goose configure",
                approval_mode_ids={"ask": "approve", "bypass": "auto"},
                config_root_env="GOOSE_PATH_ROOT",
                config_root_default="~/.config/goose",
                config_xdg_dir="goose",
                config_watch_files=("config.yaml", "permission.yaml", "config/config.yaml", "config/permission.yaml"),
                auth_method_ids=("goose-provider",),
                workspace=WorkspaceConventions(instructions_file=".goosehints"),
                quirks=AgentQuirks(
                    supports_session_resume=False,
                    supports_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="approval",
                    model_switch="config_option",
                ),
                notes=(
                    "2026-09-27 官方 ACP 目录 1.52.0：goose acp；源码 "
                    "04ed836c 提供 load、mode/model/thinking 配置；未做本机推理验收。",
                    "项目规则使用 .goosehints，需要启用原生 Developer 扩展。",
                    "原生 auto 允许全部工具，仅对应完整权限；smart_approve 无法等同"
                    "仅自动编辑，chat 禁用全部工具，均不冒充通用权限档。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="cursor",
                label="Cursor CLI",
                command=("cursor-agent", "acp"),
                executable="cursor-agent",
                setup_url="https://cursor.com/docs/cli/acp",
                login_command="agent login",
                config_root_env="CURSOR_CONFIG_DIR",
                config_root_default="~/.cursor",
                workspace_config_dir=".cursor",
                config_watch_files=("cli-config.json", "cli.json", "mcp.json"),
                env_keys_hint=("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN"),
                auth_method_ids=("cursor_login",),
                client_methods={
                    "questions": "cursor/ask_question",
                    "plan": "cursor/create_plan",
                    "todos": "cursor/update_todos",
                    "task": "cursor/task",
                    "image": "cursor/generate_image",
                },
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=False,
                    supports_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="none",
                    model_switch="config_option",
                    thought_chunks=False,
                ),
                notes=(
                    "2026-09-27 官方 ACP 目录 2026.09.18 使用 cursor-agent acp，"
                    "文档中的 agent acp 是同一 CLI 的入口；未做本机推理验收。",
                    "agent/plan/ask 是运行模式，工具审批通过 session/request_permission。",
                    "原生问答、计划和图片使用额外 ACP 方法，需客户端接收；"
                    "团队级 MCP 暂不在其 ACP 面支持范围。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="copilot",
                label="GitHub Copilot CLI",
                command=("copilot", "--acp", "--stdio"),
                executable="copilot",
                install_command="npm install -g @github/copilot",
                setup_url="https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server",
                login_command="copilot login",
                config_root_env="COPILOT_HOME",
                config_root_default="~/.copilot",
                workspace_config_dir=".github",
                config_watch_files=("settings.json", "config.json", "providers.json", "mcp-config.json"),
                env_keys_hint=("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_PROVIDERS_CONFIG"),
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=False,
                    supports_session_load=True,
                    supports_set_mode=False,
                    mode_semantics="none",
                    model_switch="config_option",
                    thought_chunks=False,
                ),
                notes=(
                    "2026-09-27 官方 ACP 目录 1.0.88 与官方文档确认 --acp --stdio；"
                    "未做本机推理验收。",
                    "ACP session/new 不携带工具过滤或初始推理强度；"
                    "只暴露会话实际返回的可变配置，不将启动参数伪装成热切换。",
                    "模型与 BYOK 路由由原生 CLI 和 providers.json 管理；"
                    "文件监测只读取元数据。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="devin",
                label="Devin",
                command=("devin", "acp"),
                executable="devin",
                setup_url="https://docs.devin.ai/cli/acp/zed",
                login_command="devin auth login",
                config_root_default="~/.config/devin",
                config_xdg_dir="devin",
                workspace_config_dir=".devin",
                config_watch_files=("config.json", "config.local.json", "mcp_config.json", "mcp_config.local.json"),
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=False,
                    supports_session_load=False,
                    supports_set_mode=False,
                    mode_semantics="approval",
                    model_switch="config_option",
                    thought_chunks=False,
                ),
                notes=(
                    "2026-09-27 官方 ACP 目录 3000.11.3：devin acp；"
                    "会话能力按 initialize 与 configOptions 协商，未做本机推理验收。",
                    "原生 CLI 自动读项目 AGENTS.md 与 .devin 配置；"
                    "登录、订阅与组织策略仍由 Devin 管理。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="omp",
                session_creation_proves_auth=False,
                label="omp (oh-my-pi)",
                command=("omp", "acp"),
                executable="omp",
                install_command="brew install can1357/tap/omp",
                setup_url="https://github.com/can1357/oh-my-pi",
                login_command="omp",
                config_root_env="PI_CODING_AGENT_DIR",
                config_root_default="~/.omp/agent",
                workspace_config_dir=".omp",
                config_watch_files=("config.yml", "config.yaml", "settings.json", "models.yml", "models.yaml", "models.json", "models.jsonc"),
                auth_method_ids=("agent",),
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=True,
                    supports_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="none",
                    model_switch="config_option",
                    needs_client_fs=True,
                ),
                notes=(
                    "2026-09-27 官方源码 baf63aa1：omp acp；"
                    "model/thinking/mode 分列 configOptions，模型 id 是 provider/model。",
                    "default/plan 是运行模式；写入和执行走 ACP 审批，"
                    "文件与终端能力按客户端声明协商；未做本机推理验收。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="grok",
                label="Grok Build",
                command=("grok", "--no-auto-update", "agent", "stdio"),
                executable="grok",
                install_command="npm install -g @xai-official/grok",
                setup_url="https://docs.x.ai/build/cli/headless-scripting",
                login_command="grok login",
                config_root_env="GROK_HOME",
                config_root_default="~/.grok",
                config_watch_files=("config.toml",),
                env_keys_hint=("XAI_API_KEY",),
                auth_method_ids=("xai.api_key", "cached_token"),
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=False,
                    supports_session_load=False,
                    supports_set_mode=False,
                    mode_semantics="none",
                    model_switch="config_option",
                    thought_chunks=False,
                ),
                notes=(
                    "2026-09-27 官方 ACP 目录 1.0.41 与 headless 文档："
                    "grok --no-auto-update agent stdio；未做本机推理验收。",
                    "仅复用 CLI 的本地登录或显式传入的 XAI_API_KEY；"
                    "官方 ACP 流程需先选择其提供的非交互认证方法。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="crush",
                session_creation_proves_auth=False,
                label="Crush",
                command=(
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "crush_bridge" / "cli.py"),
                ),
                executable="crush",
                install_command="brew install charmbracelet/tap/crush",
                setup_url="https://github.com/charmbracelet/crush",
                login_command="crush",
                config_root_default="~/.config/crush",
                config_xdg_dir="crush",
                config_watch_files=("crush.json", "crushrc"),
                workspace_config_dir=".crush",
                elicitation_forms=True,
                approval_option_id="_approval",
                approval_mode_ids={"ask": "ask", "bypass": "bypass"},
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=False,
                    supports_session_load=True,
                    prefer_session_load=True,
                    supports_set_mode=False,
                    mode_semantics="none",
                    model_switch="config_option",
                ),
                notes=(
                    "2026-09-27：通过 Kaus 自写桥连接原生 HTTP v1/SSE，"
                    "未复制或分发上游实现；原生 Crush 由用户独立安装。",
                    "coder/plan 为运行模式；审批项 _approval 单独选择 ask/bypass。"
                    "桥把模型和 MCP 设置写入每会话私有目录，保留原生配置。",
                    "上游当前 FSL-1.1-MIT 含竞品用途限制，不能按无条件 MIT 处理；"
                    "商业集成与分发须另行审查上游许可。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="commandcode",
                session_creation_proves_auth=False,
                label="Command Code",
                command=(
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "commandcode_bridge" / "cli.py"),
                ),
                executable="command-code",
                install_command="npm install -g command-code",
                setup_url="https://commandcode.ai/docs/headless",
                login_command="command-code login",
                config_root_default="~/.commandcode",
                config_watch_files=("config.json", "settings.json", "settings.local.json", "providers.json", "mcp.json"),
                workspace_config_dir=".commandcode",
                env_keys_hint=("COMMAND_CODE_API_KEY",),
                approval_mode_ids={"plan": "plan", "bypass": "yolo"},
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=True,
                    supports_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="approval",
                    model_switch="config_option",
                    mcp_via_session_new=False,
                ),
                notes=(
                    "2026-09-27 原生 1.66.0：通过 Kaus 自写桥读取官方 headless NDJSON；"
                    "会话按原生 sessionId 精确恢复，load 仅回放桥保存的记录，不发送提示词。",
                    "默认 plan；只有用户明确选完整权限才使用 yolo。原生 headless 没有"
                    "审批/问答回传，不暴露 ask/auto；项目 MCP 投影明确不支持。",
                    "原生包为 UNLICENSED，桥不复制原生实现；登录仍由用户的 CLI 管理。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="alma",
                session_creation_proves_auth=False,
                label="Alma",
                command=(
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "alma_bridge" / "cli.py"),
                ),
                executable="alma",
                setup_url="https://alma.now/",
                env_keys_hint=("ALMA_API_URL",),
                client_methods={"questions": "_alma/questions"},
                approval_mode_ids={"read_only": "chat", "bypass": "tools"},
                quirks=AgentQuirks(
                    supports_session_resume=False,
                    supports_session_load=True,
                    prefer_session_load=True,
                    supports_set_mode=True,
                    supports_set_model=True,
                    mode_semantics="approval",
                    model_switch="set_model",
                    mcp_via_session_new=False,
                ),
                notes=(
                    "2026-09-27：通过 Kaus 原创桥连接原生 loopback REST/WS；"
                    "依据官方 v0.4.148 服务包的公开本地协议，未复制原生实现。",
                    "默认 chat 每轮显式 noTools；tools 只在用户选完整权限后启用，"
                    "沿用原生工具策略，不将其冒充 ask/auto。模型来自原生目录。",
                    "原生安装与登录由用户管理；本机未安装原生 Alma，未完成原生推理验收。",
                ),
            )
        ),
        _preset(
            AcpPreset(
                id="zcode",
                label="ZCode",
                command=(
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "zcode_bridge" / "cli.py"),
                ),
                executable="zcode",
                setup_url="https://github.com/zai-org/ZCode",
                session_creation_proves_auth=False,
                elicitation_forms=True,
                thought_option_id="thought_level",
                approval_mode_ids={"plan": "plan", "ask": "build", "auto": "edit", "bypass": "yolo"},
                config_root_env="ZCODE_DATA_BASE_DIR",
                config_root_default="~",
                config_watch_files=(".zcode/v2/provider_config.json",),
                workspace_config_dir="",
                workspace=WorkspaceConventions(instructions_file="AGENTS.md"),
                quirks=AgentQuirks(
                    supports_session_resume=True,
                    supports_session_load=True,
                    supports_set_mode=True,
                    mode_semantics="approval",
                    model_switch="config_option",
                    mcp_via_session_new=True,
                ),
                notes=(
                    "2026-09-27：Kaus 原创桥连接原生 zcode app-server --stdio；"
                    "官方 Apache-2.0 源码独立构建，普通对话和组长共用该桥。",
                    "模型 id 原样保留 provider/model 二元组；plan/build/edit/yolo"
                    "沿用原生权限语义，不暴露尚未实现的原生 auto 模式。",
                    "每会话独立原生存储并精确恢复；本机原生创建/恢复及隔离验证通过，"
                    "本地服务确定性测试与真实云模型推理分开记录。",
                    "provider_config.json 从原生 DATA_BASE 根观察文件元数据；"
                    "旧版 ~/.zcode/cli/config.json 仍由原生读取，未纳入第二根监听。",
                ),
            )
        ),
    )
)


class UnknownPresetError(KeyError):
    """``backends[].preset`` 写了一个目录里没有的 id。"""


def preset_ids() -> tuple[str, ...]:
    return tuple(sorted(PRESETS))


def get_preset(preset_id: str) -> AcpPreset:
    """按 id 取，未知 id 抛 :class:`UnknownPresetError`（调用方跳过该条并警告）。"""
    try:
        return PRESETS[preset_id]
    except KeyError as exc:
        raise UnknownPresetError(
            f"未知的 ACP 预设 {preset_id!r}；可用：{list(preset_ids())}"
        ) from exc


def with_quirks(preset: AcpPreset, **overrides: bool) -> AcpPreset:
    """造一个只改了几位怪癖的副本（测试与临时排障用，不写回目录）。"""
    return replace(preset, quirks=replace(preset.quirks, **overrides))


__all__ = [
    "AUTH_MODEL",
    "QUIRK_BITS",
    "SESSION_ID_PLACEHOLDER",
    "AcpPreset",
    "AgentQuirks",
    "WorkspaceConventions",
    "PRESETS",
    "UnknownPresetError",
    "get_preset",
    "preset_ids",
    "with_quirks",
]
