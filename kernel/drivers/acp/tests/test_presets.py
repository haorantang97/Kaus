"""预设目录本身的断言（批次三十二 / AD-156）。

这个文件与 ``presets.py`` 一样，是 ``test_purity.py`` 那两条产品名扫描的**豁免**
（``CATALOG_TEST``）：只读目录内容的测试必须能写出目录里的 id，否则它没法断言任何
东西。豁免只到这里为止——本包其余文件仍不得出现任何预设 id 的字面量。

取证结论落进目录（AD-156）
--------------------------

这个文件守的不是「代码能跑」，而是「**这几个值有出处**」。批次二十五写下的怪癖
表是按公开文档与保守原则猜的（AD-151 承认了这一点），批次三十二把七个适配器在
一台没有任何凭据的机器上各拉起来一次，用它们自己在 ``initialize`` / ``session/new``
里说的话把其中几位钉死。逐字记录在 ``docs/forensics/acp-adapters-2026-09-06.md``。

**为什么这些断言值得存在。** 它们不测试行为，测试的是「有人后来把某一位改回猜
测值时会红」。取证的成本很高（要装七个 npm 包、跑七次握手），而一个手滑的默认值
改动是零成本的——两者不对称，所以取证结论要有断言守着。

被钉死的那几位一律附来源：``initialize`` 的哪个字段、或者哪一次真调用的返回。
没有来源的位（``tool_update_cumulative`` / ``thought_chunks`` / ``needs_client_fs``）
这里**一个都不断言**——给一个没取证过的值加测试，等于把猜测升格成事实。
"""

from __future__ import annotations

from drivers.acp.capabilities import effective_quirks, resolve_mode_id
from drivers.acp.presets import PRESETS, QUIRK_BITS, get_preset


NEW_NATIVE_ACP_IDS = {"goose", "cursor", "copilot", "devin", "omp", "grok"}


def test_native_acp_commands_match_official_entrypoints() -> None:
    """固定来源：2026-09-27 官方目录及厂商文档，详见阶段 sources-catalog.md。"""
    assert {id: get_preset(id).command for id in NEW_NATIVE_ACP_IDS} == {
        "goose": ("goose", "acp"),
        "cursor": ("cursor-agent", "acp"),
        "copilot": ("copilot", "--acp", "--stdio"),
        "devin": ("devin", "acp"),
        "omp": ("omp", "acp"),
        "grok": ("grok", "--no-auto-update", "agent", "stdio"),
    }


def test_new_catalog_rows_do_not_mislabel_source_review_as_live() -> None:
    for id in NEW_NATIVE_ACP_IDS:
        preset = get_preset(id)
        assert not preset.verified_bits
        assert "verifiedBits" not in preset.to_wire()
        assert preset.resume_argv_template is None


def test_task_modes_do_not_become_permission_modes() -> None:
    # Cursor agent/plan/ask 与 omp default/plan 是任务模式，审批另走 ACP 请求。
    for id in ("cursor", "omp"):
        preset = get_preset(id)
        assert preset.quirks.mode_semantics == "none"
        assert preset.quirks.supports_set_mode is True
    assert get_preset("omp").quirks.model_switch == "config_option"


def test_local_session_registration_keeps_auth_and_permission_boundaries() -> None:
    for id in ("crush", "commandcode", "alma", "zcode", "omp", "deepseek-acp"):
        assert get_preset(id).session_creation_proves_auth is False
    preset = get_preset("zcode")
    assert preset.command[-1].endswith("/zcode_bridge/cli.py")
    assert preset.approval_mode_ids == {"plan": "plan", "ask": "build", "auto": "edit", "bypass": "yolo"}
    assert "auto" not in preset.approval_mode_ids.values()
    assert preset.quirks.model_switch == "config_option"
    assert preset.workspace.instructions_file == "AGENTS.md"
    assert preset.elicitation_forms


def test_native_file_scope_and_reasoning_options_are_explicit() -> None:
    preset = get_preset("deepseek-acp")
    assert preset.thought_option_id == "reasoning"
    assert preset.approval_option_id == "sandbox"
    assert preset.approval_mode_ids == {"read_only": "read-only", "bypass": "danger-full-access"}
    assert preset.quirks.mode_semantics == "none"
    assert preset.config_root_env == "DSH_HOME"
    assert preset.login_command == "deepseek-acp --setup"


def test_setup_metadata_is_exposed_without_automatic_installation() -> None:
    for preset in PRESETS.values():
        wire = preset.to_wire()
        assert wire["setupUrl"].startswith("https://")
        assert wire["executable"] == preset.executable
        assert " " not in wire["executable"]
        assert wire.get("installCommand") == preset.install_command
    assert get_preset("pi").install_command == (
        "npm install -g @earendil-works/pi-coding-agent pi-acp"
    )


def test_new_config_watchers_follow_native_provider_configuration() -> None:
    assert get_preset("goose").config_root_env == "GOOSE_PATH_ROOT"
    assert "config/config.yaml" in get_preset("goose").config_watch_files
    assert get_preset("copilot").config_root_env == "COPILOT_HOME"
    assert {"settings.json", "providers.json"} <= set(get_preset("copilot").config_watch_files)
    assert get_preset("cursor").config_root_env == "CURSOR_CONFIG_DIR"
    assert get_preset("cursor").workspace_config_dir == ".cursor"
    assert get_preset("omp").config_root_env == "PI_CODING_AGENT_DIR"
    assert {"models.yml", "models.yaml", "models.json"} <= set(get_preset("omp").config_watch_files)
    assert get_preset("devin").workspace.instructions_file == "AGENTS.md"


def test_every_preset_row_declares_its_auth_method_ids_field() -> None:
    """新字段是元组、且只有 id 形状的字符串（不带空格、不是句子）。"""
    for preset in PRESETS.values():
        assert isinstance(preset.auth_method_ids, tuple)
        for method_id in preset.auth_method_ids:
            assert isinstance(method_id, str) and method_id
            assert " " not in method_id, f"{preset.id}: {method_id!r} 看起来是名字不是 id"


def test_the_auth_method_ids_we_actually_measured() -> None:
    """取证抄回来的那几行，逐字（``docs/forensics/acp-adapters-2026-09-06.md`` §2）。"""
    assert get_preset("codex").auth_method_ids == (
        "chatgpt",
        "codex-api-key",
        "openai-api-key",
    )
    assert get_preset("opencode").auth_method_ids == ("opencode-login",)
    assert get_preset("gemini").auth_method_ids == (
        "oauth-personal",
        "gemini-api-key",
        "vertex-ai",
        "gateway",
    )
    assert get_preset("qwen").auth_method_ids == ("openai",)
    assert get_preset("pi").auth_method_ids == ("pi_terminal_login",)


def test_claude_code_declares_no_acp_side_login_method() -> None:
    """它的 ``authMethods`` 是**空数组**（0.75.1 实测）——空不是「没取证」。

    这一位的产品含义是：ACP 面上没有可点的登录方式，登录态整个来自 CLI 的家目录。
    所以「去登录」这条路对它只能指向终端，而不是列一串 id 让用户挑。
    """
    assert get_preset("claude-code").auth_method_ids == ()


def test_set_mode_and_set_model_where_we_really_called_them() -> None:
    """这两位在 ``initialize`` 里没有声明面，只能真调一次才知道（AD-156）。

    - ``claude-code`` 0.75.1：``set_mode`` 回 ``{}``，``set_model`` 回 ``-32601``；
    - ``opencode`` 1.18.29：批次三十二拿**当前值**空跑，两个都回 ``{}``；批次
      三十四在中继真机上带着真实会话再跑一次，``session/new`` 一个 mode 都没报、
      ``set_mode`` 回 ``-32602``——所以这一位改回 ``False``（AD-158）。空跑当前值
      是个假阳性来源，记在这里是为了下次不再上同一个当。
    """
    claude_code = get_preset("claude-code").quirks
    assert claude_code.supports_set_mode is True
    assert claude_code.supports_set_model is False

    opencode = get_preset("opencode").quirks
    assert opencode.supports_set_mode is False
    assert opencode.supports_set_model is True


def test_resume_is_off_where_the_agent_never_declares_it() -> None:
    """两行按「没声明就别声明」收敛（AD-151 的保守规则）。

    - ``pi`` 0.0.33 的 ``sessionCapabilities`` 是 ``{list, delete}``——明说了没有
      ``resume``，所以哪怕预设写 True 也会被 :func:`effective_quirks` 改回来；
    - ``gemini`` 0.58.0 **整段 ``sessionCapabilities`` 都没有**，那时预设是唯一
      的来源、不会被自动纠正，所以预设自己必须是保守值。
    """
    assert get_preset("pi").quirks.supports_session_resume is False
    assert get_preset("gemini").quirks.supports_session_resume is False

    # gemini 那一份 initialize 的形状（只留与本条有关的键）：预设填什么就是什么。
    gemini_initialize = {"agentCapabilities": {"loadSession": True}}
    effective = effective_quirks(get_preset("gemini").quirks, gemini_initialize)
    assert effective.supports_session_resume is False


def test_a_declared_session_capabilities_block_still_wins_over_the_preset() -> None:
    """实测压过声明的方向没变（AD-151）：agent 说有 resume，预设的 False 让位。"""
    declared = {
        "agentCapabilities": {
            "loadSession": True,
            "sessionCapabilities": {"list": {}, "resume": {}, "close": {}},
        }
    }
    effective = effective_quirks(get_preset("gemini").quirks, declared)
    assert effective.supports_session_resume is True
    assert effective.supports_session_load is True


# ---------------------------------------------------------------- 批次三十四


def test_the_three_new_rows_from_the_relay_run() -> None:
    """中继真机跑通的三行进了目录（AD-158），且各自的形状逐字对得上。

    这三行是**新**的，所以这里断言的是「有没有」与「怎么拉起来」；它们的怪癖位
    另有几条用例分头守着。
    """
    dsh = get_preset("dsh")
    assert dsh.command == ("dsh", "--profile", "acp")
    assert dsh.env_keys_hint == ("DSH_HOME", "DEEPSEEK_API_KEY")

    deepseek = get_preset("deepseek-acp")
    assert deepseek.command == ("npx", "-y", "deepseek-acp")
    assert deepseek.env_keys_hint == ("DEEPSEEK_API_KEY",)

    kilo = get_preset("kilo")
    assert kilo.command == ("kilo", "acp")

    # 三行都没有实测过的续接命令 → 「在 CLI 里打开」这个入口一律不显示（AD-71）。
    for preset in (dsh, deepseek, kilo):
        assert preset.resume_argv_template is None
        assert preset.auth_method_ids == (), "authMethods 实测是空数组（靠 env）"


def test_codex_points_at_the_maintained_package() -> None:
    """AD-156 留的那条待办结了：换到上游现在维护的那个包（AD-158）。

    换包这件事批次三十二**故意没做**（「当前命令仍工作，换包要真机验证」）；
    本批测试员在云机上把新包整套跑了一遍，所以现在换得起。
    """
    assert get_preset("codex").command == (
        "npx",
        "@agentclientprotocol/codex-acp@latest",
    )


def test_the_engines_that_send_incremental_tool_output() -> None:
    """AD-49 的默认值有反例，而且**只有这两行**（AD-158 / AD-160）。

    这条断言的价值在那个等号：这一位一旦被顺手改成 False，工具卡上就会出现重复
    文本，而那种症状在页面上很难一眼归因到某一行预设；反过来，把实测出来的 False
    改回 True，同一张卡上会**少**掉后半截输出。两个方向都要有人守。
    """
    incremental = {
        preset.id
        for preset in PRESETS.values()
        if not preset.quirks.tool_update_cumulative
    }
    assert incremental == {"claude-code", "openclaw"}


def test_thought_level_engines_never_map_the_approval_mode() -> None:
    """这两家的 mode 是**思考档**，不是审批档（AD-158）。

    真机原话：pi 报 ``off/minimal/low/medium/high/xhigh``，openclaw 报
    ``off/minimal/low/medium/high/adaptive``。把 Binding 的「自动批准」映射到
    这里，用户按下的开关会变成「多想一会儿」——一次在界面上完全看不见的错位。
    """
    thought_level = {
        preset.id
        for preset in PRESETS.values()
        if preset.quirks.mode_semantics == "thought_level"
    }
    assert thought_level == {"pi", "openclaw"}
    for preset_id in thought_level:
        assert get_preset(preset_id).quirks.supports_set_mode is True


def test_effort_suffix_and_close_before_resume_stay_where_we_measured_them() -> None:
    """这两位都只在**实测撞到过**的那一行上（AD-158）。

    它们的代价是不对称的：多标一行 ``effort_suffix``，那台引擎收到的每个 modelId
    都会多一截它不认识的后缀；多标一行 ``resume_requires_close``，每次续接都会多
    一次注定 -32601 的 RPC。所以这两位不允许「顺手也给别家加上」。
    """
    assert {
        preset.id
        for preset in PRESETS.values()
        if preset.quirks.model_id_format == "effort_suffix"
    } == {"codex"}
    assert {
        preset.id
        for preset in PRESETS.values()
        if preset.quirks.resume_requires_close
    } == {"dsh"}


def test_config_option_is_the_only_model_switch_for_the_engines_without_set_model() -> None:
    """``session/set_model`` 回 -32601 的那几家，换模型走 configOptions（AD-158）。"""
    measured_ids = {
        "claude-code", "codex", "opencode", "gemini", "antigravity", "qwen",
        "openclaw", "pi", "hermes-acp", "dsh", "deepseek-acp", "kilo",
    }
    assert {
        preset.id
        for preset in PRESETS.values()
        if preset.id in measured_ids and preset.quirks.model_switch == "config_option"
    } == {"claude-code", "pi", "dsh", "deepseek-acp", "kilo"}
    assert {
        preset.id
        for preset in PRESETS.values()
        if preset.id in measured_ids and preset.quirks.model_switch == "set_model"
    } == {"codex", "opencode", "qwen", "antigravity", "hermes-acp"}


def test_repaired_resume_support_does_not_remove_the_old_version_guard() -> None:
    """0.9 原生续问已通过；0.8 的已复现缺陷仍按版本禁用。"""
    preset = get_preset("deepseek-acp")
    assert preset.quirks.supports_session_resume is True
    assert preset.quirks.supports_session_load is True
    assert preset.quirks.prefer_session_load is True
    broken = effective_quirks(preset.quirks, DEEPSEEK_08_INITIALIZE, preset)
    assert broken.supports_session_resume is False
    assert broken.supports_session_load is False


# ---------------------------------------------------------------- 批次三十七


#: 那一版自己报的 initialize：声明里 resume 与 loadSession 都是有的（真机取证，
#: AD-158），可两条 RPC 都回 -32603——AD-164 治的就是这个矛盾。
DEEPSEEK_08_INITIALIZE: dict = {
    "agentInfo": {"name": "deepseek-acp", "version": "0.8.0"},
    "agentCapabilities": {
        "loadSession": True,
        "sessionCapabilities": {"list": {}, "resume": {}, "close": {}},
    },
}


def _with_version(version: str | None) -> dict:
    payload = {
        "agentCapabilities": DEEPSEEK_08_INITIALIZE["agentCapabilities"],
    }
    if version is not None:
        payload["agentInfo"] = {"name": "deepseek-acp", "version": version}
    return payload


def test_a_known_bad_version_forces_the_bits_off_despite_the_agents_claim() -> None:
    """AD-164：已复现的版本缺陷压过 initialize 的自报——**只往关的方向**。"""
    preset = get_preset("deepseek-acp")
    effective = effective_quirks(preset.quirks, DEEPSEEK_08_INITIALIZE, preset)
    assert effective.supports_session_resume is False
    assert effective.supports_session_load is False


def test_the_prefix_matches_the_whole_patch_line() -> None:
    """一个已复现的实现缺陷通常横跨整条补丁线，逐个版本号登记只会漏。"""
    preset = get_preset("deepseek-acp")
    for version in ("0.8.0", "0.8.3", "0.8.12"):
        effective = effective_quirks(preset.quirks, _with_version(version), preset)
        assert effective.supports_session_resume is False, version


def test_another_version_is_not_touched() -> None:
    """表里没登记的版本一位都不关——它说它行，我们就当它行（AD-151 的原方向）。"""
    preset = get_preset("deepseek-acp")
    effective = effective_quirks(preset.quirks, _with_version("0.9.0"), preset)
    assert effective.supports_session_resume is True
    assert effective.supports_session_load is True


def test_an_unreported_version_forces_nothing() -> None:
    """读不到 ``agentInfo.version`` 时一位都不关：不知道装的是哪一版，就不替它做决定。"""
    preset = get_preset("deepseek-acp")
    effective = effective_quirks(preset.quirks, _with_version(None), preset)
    assert effective.supports_session_resume is True


def test_known_bad_can_only_turn_bits_off() -> None:
    """版本例外只能禁用列出的布尔位，不能覆盖最新版本或其他能力。"""
    for preset in PRESETS.values():
        for prefix, bits in preset.known_bad.items():
            assert bits, f"{preset.id} 的 known_bad[{prefix!r}] 是空的"
            effective = effective_quirks(preset.quirks, {"agentInfo": {"version": prefix}}, preset)
            for bit in bits:
                assert bit in QUIRK_BITS, f"{preset.id}: 未知的怪癖位 {bit}"
                assert getattr(effective, bit) is False
            for name, value in vars(preset.quirks).items():
                if name not in bits:
                    assert getattr(effective, name) == value, (preset.id, name)


def test_only_the_row_with_reproduced_evidence_has_a_known_bad_entry() -> None:
    """这张表是**已复现的缺陷**，不是「我担心它有问题」——空着才是常态。"""
    rows = {preset.id for preset in PRESETS.values() if preset.known_bad}
    assert rows == {"deepseek-acp"}


# ---------------------------------------------------------------- 批次三十六


#: ``session/new`` 报回来的审批档，逐字（0.75.1 中继真机，AD-160）。写成常量是
#: 因为下面两条用例问的是**同一份**列表的两件事：映射表挑得中哪一个、挑不中的
#: 那一档会不会被悄悄映射到别的东西上。
CLAUDE_CODE_MODE_IDS: tuple[str, ...] = (
    "default",
    "acceptEdits",
    "plan",
    "auto",
    "bypassPermissions",
)


def test_claude_code_has_every_quirk_bit_verified_on_a_real_engine() -> None:
    """七位全部有真机依据（AD-160）——目录里第二行做到这个的。

    这条断言守的不是行为，是**取证等级**：`verified_bits` 一旦被删掉几位，能力
    矩阵上对应的格子会从 `live` 悄悄退回 `declared`，界面上一个字都不变。
    """
    from drivers.acp.presets import ALL_MEASURED_BITS

    preset = get_preset("claude-code")
    assert preset.verified_bits == ALL_MEASURED_BITS

    quirks = preset.quirks
    # 中继真机那一轮的逐项结论（`CLAUDE-CODE-QUIRK-DELTA.md` 的表）。
    assert quirks.tool_update_cumulative is False
    assert quirks.thought_chunks is True
    assert quirks.supports_set_mode is True and quirks.mode_semantics == "approval"
    assert quirks.supports_set_model is False
    assert quirks.model_switch == "config_option"
    assert quirks.supports_session_resume is True
    assert quirks.supports_session_load is True
    # 登录仍然只能在终端做，命令没变（AD-157 的那一列）。
    assert preset.login_command == "claude"


def test_claude_code_approval_modes_map_onto_the_ids_it_actually_reports() -> None:
    """审批档映射：ask → ``default``、auto → ``bypassPermissions``（AD-160）。

    它报**五**档，其中三档都能被当成「自动」（`acceptEdits` / `auto` /
    `bypassPermissions`），所以「挑得中」不够——还得挑中**对**的那一个。映射表
    是按候选顺序取第一个命中项的，这条用例把那个顺序在这一行上钉死：用户按下
    「自动批准」拿到的必须是真正全放行的那一档，而不是只放行编辑的那一档。
    """
    from drivers.acp.capabilities import resolve_mode_id

    assert get_preset("claude-code").quirks.mode_semantics == "approval"
    assert resolve_mode_id("ask", CLAUDE_CODE_MODE_IDS) == "default"
    assert resolve_mode_id("auto", CLAUDE_CODE_MODE_IDS) == "bypassPermissions"
    # 「一律不批」在它这里就是 plan（只读那一档），不是另外四档里的任何一个。
    assert resolve_mode_id("deny", CLAUDE_CODE_MODE_IDS) == "plan"


def test_claude_code_model_slots_reach_the_catalog_verbatim() -> None:
    """它的模型选项是**档位 id**，不是模型名——原样进目录（AD-160）。

    `configOptions[category=model]` 的取值是 `default` / `opus` / `sonnet` /
    `haiku` 这样的槽位。把它们「翻译」成模型名（或反过来去猜背后是哪个模型）需要
    我们替引擎做一串假设，而中继路线下同一个槽位背后完全可能是另一家的模型。
    所以纪律与那家的 JSON 元组取值同源：**原样列出、原样送回，一个字都不解析**，
    显示名用它自己给的那一份。
    """
    from drivers.acp.capabilities import config_options_from_session_result

    session_result = {
        "sessionId": "sess-1",
        "configOptions": [
            {
                "id": "model",
                "name": "Model",
                "category": "model",
                "type": "select",
                "currentValue": "default",
                "options": [
                    {"value": "default", "name": "Default"},
                    {"value": "opus", "name": "Opus"},
                    {"value": "sonnet", "name": "Sonnet"},
                    {"value": "haiku", "name": "Haiku"},
                ],
            }
        ],
    }
    parsed = config_options_from_session_result(session_result)
    assert [m.model_id for m in parsed.models] == [
        "default",
        "opus",
        "sonnet",
        "haiku",
    ]
    assert [m.display_name for m in parsed.models] == [
        "Default",
        "Opus",
        "Sonnet",
        "Haiku",
    ]
    assert parsed.current_model_id == "default"
    # 换模型时发的是这一项自己的 id（按 category 找、按 id 发，AD-158）。
    assert parsed.model_option_id == "model"
    assert get_preset("claude-code").quirks.model_switch == "config_option"


def test_claude_code_relay_route_is_recorded_by_name_only() -> None:
    """中继路线只留**变量名**，一个值都不留（AD-10 / AD-48 / 安全红线）。

    这台引擎换 provider 的办法是把两个变量指到一条 Anthropic 兼容端点上——这件事
    值得写进备注（否则「怎么它答得像另一家」没人说得清），但备注里出现的必须只有
    名字：目录是会被打包、被贴进报告、被上 wire 的只读表。
    """
    preset = get_preset("claude-code")
    # 结构化的那一份（界面/文档念的就是它），与备注里那句话说的是同两个名字。
    assert preset.env_keys_hint == ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
    notes = " ".join(preset.notes)
    assert "ANTHROPIC_BASE_URL" in notes and "ANTHROPIC_AUTH_TOKEN" in notes
    # 只有名字：不出现赋值、不出现 URL、不出现任何看起来像 token 的东西。
    assert "ANTHROPIC_BASE_URL=" not in notes
    assert "ANTHROPIC_AUTH_TOKEN=" not in notes
    assert "http://" not in notes and "https://" not in notes
    assert "sk-" not in notes


def test_verified_bits_only_name_real_quirk_bits() -> None:
    """取证等级写错一个名字就会静默失效，所以这里机械核一遍（AD-158）。"""
    from drivers.acp.presets import QUIRK_BITS

    for preset in PRESETS.values():
        assert isinstance(preset.verified_bits, frozenset)
        unknown = preset.verified_bits - set(QUIRK_BITS)
        assert not unknown, f"{preset.id}: {unknown}"


def test_the_rows_we_measured_end_to_end_have_every_bit_verified() -> None:
    """七位全实测的就这两行——别的行不许假装也全测过（AD-158 / AD-160）。

    批次三十四一行，批次三十六又添一行。这个等号的方向是双向的：漏掉一行说明
    取证结论没落进目录，多出一行说明有人把「还没测」写成了「测过」。
    """
    from drivers.acp.presets import ALL_MEASURED_BITS

    full = {
        preset.id
        for preset in PRESETS.values()
        if preset.verified_bits == ALL_MEASURED_BITS
    }
    assert full == {"claude-code", "codex"}


def test_rows_the_relay_never_reached_stay_declared() -> None:
    """按计划跳过的那一行一位都没升 live（AD-158）。

    空集合的含义是「没测过」，不是「测出来不支持」——两者混在一起，界面上就会把
    「还不知道」画成「已知不行」。
    """
    assert get_preset("gemini").verified_bits == frozenset()


def test_forensic_notes_are_attributed_to_a_version() -> None:
    """写进 ``notes`` 的取证结论必须带版本号——不带版本的「实测」过两个月就是谣言。"""
    for preset in PRESETS.values():
        for note in preset.notes:
            if note.startswith("取证"):
                head = note.split("：", 1)[0]
                assert any(ch.isdigit() for ch in head), (
                    f"{preset.id} 的取证备注没有版本号：{note!r}"
                )


def test_no_preset_claims_a_bit_nobody_has_measured() -> None:
    """批次四十三：新加的怪癖位在**每一行**上都停在 declared。

    这条断言守的是「加一位新怪癖时不许顺手把它算成测过了」：写
    ``verified_bits=frozenset(QUIRK_BITS)`` 的那两行会静默把新位一起收进去，
    于是能力矩阵上凭空多出一格 live，而界面上一个字都不变。
    """
    from drivers.acp.presets import ALL_MEASURED_BITS, QUIRK_BITS, UNMEASURED_BITS

    assert UNMEASURED_BITS <= set(QUIRK_BITS)
    assert ALL_MEASURED_BITS == frozenset(QUIRK_BITS) - UNMEASURED_BITS
    for preset in PRESETS.values():
        claimed = preset.verified_bits & UNMEASURED_BITS
        assert not claimed, f"{preset.id} 声称测过了还没人测过的位：{claimed}"


def test_mcp_projection_excludes_adapters_that_only_store_the_servers() -> None:
    """pi-acp 上游明确忽略传入的服务器，不能报告项目 MCP 已投射。"""
    for preset in PRESETS.values():
        expected = preset.id not in {"pi", "openclaw", "commandcode", "alma"}
        assert preset.quirks.mcp_via_session_new is expected
        assert preset.quirks.to_wire()["mcpViaSessionNew"] is expected


def test_gateway_session_uuid_uses_load_for_cross_process_recovery() -> None:
    """2026.9.6 真机：resume 拒绝 ACP UUID，load 可恢复同一会话。"""
    assert get_preset("openclaw").quirks.prefer_session_load is True
    assert get_preset("openclaw").quirks.supports_session_load is True


# ---------------------------------------------------------------- 批次四十九


def test_the_antigravity_row_copies_the_official_registry_entry() -> None:
    """AD-170：这一行的每个值都来自**官方 ACP 目录清单**，不是我们的推断。

    清单（`agentclientprotocol/registry` 的 `antigravity-acp/agent.json` v1.1.1）
    里 macOS arm64 那条的 `cmd` 是 `./agy_acp_server.par`；二进制没有固定安装
    位置，所以目录只给文件名，绝对路径留给 `backends[].command` 覆盖。

    Linux 那条清单多一个 `args: ["--uid="]`，macOS 没有——**不加**。这条断言守
    的就是那个「不加」：一个只在单平台清单里出现、含义未取证的参数被顺手补进
    `command`，用户会在另一个平台上吃一个看不懂的启动失败。
    """
    preset = get_preset("antigravity")
    assert preset.command == ("agy_acp_server.par",)
    assert "--uid=" not in preset.command
    # 先交互式跑一次 `agy`，ACP 服务端复用缓存凭据（官方 headless 文档）。
    assert preset.login_command == "agy"
    # 续接命令没实测过 → 「在 CLI 里打开」这个入口一律不显示（AD-71）。
    assert preset.resume_argv_template is None
    assert preset.supports_external_cli is False


def test_the_antigravity_row_claims_nothing_it_has_not_measured() -> None:
    """只声称测过的那几位（batch54 之后：六位有依据，两位仍是 declared）。

    新一行预设最容易出的错是「顺手填得像别人一样满」：`authMethods` 抄一份猜的、
    指令文件跟着别家填一个 `AGENTS.md`、取证等级写成全测过。三样的代价各不相同，
    但方向一样——界面上会说一句我们并不知道的话。
    """
    preset = get_preset("antigravity")
    assert preset.auth_method_ids == ()
    assert preset.env_keys_hint == ()
    assert not preset.known_bad
    # 官方文档没写这家的工作目录约定 → instructions 按不支持处理，不猜文件名。
    assert preset.workspace.instructions_file is None
    assert "workspace" not in preset.to_wire()


#: `session/new` 报回来的三档，逐字（`agy_acp_server_1.1.1` 真机，batch54）。
ANTIGRAVITY_MODE_IDS: tuple[str, ...] = ("default", "auto_edit", "yolo")


def test_the_antigravity_modes_map_from_two_of_the_three_approval_modes() -> None:
    """`mode_semantics="approval"` 之后，审批档映射对这组 id 走得通吗（batch54）。

    答案是**两档通、一档不通**，这条用例把现状钉住（本批**不改映射逻辑**）：

    - `ask`  → `default`（候选表第一个就是它）；
    - `auto` → `yolo`（`acceptEdits` 对不上 `auto_edit`——下划线 vs 驼峰——所以
      直接落到 `yolo`：auto-approve **all** tools，比「只自动批准改文件」更宽，
      但方向与 `auto` 一致）；
    - `deny` → **挑不出来**（候选是 plan / readOnly / read-only / deny，它一个
      都没报）。Driver 这时不发、记一条 `set_mode_no_match` warning，会话留在
      agent 自己的默认档——不是静默，但那一档确实没生效。

    顺带钉住：`auto_edit` 这一档**没有任何公共审批档能选到它**。要让它可达，得
    给 `APPROVAL_MODE_CANDIDATES["auto"]` 加上 `auto_edit`，或者公共层多一档
    「只自动批准改文件」——两件都不在本批里。
    """
    available = [{"id": mode_id, "name": mode_id} for mode_id in ANTIGRAVITY_MODE_IDS]
    assert resolve_mode_id("ask", available) == "default"
    assert resolve_mode_id("auto", available) == "yolo"
    assert resolve_mode_id("deny", available) is None
    assert "auto_edit" not in {
        resolve_mode_id(mode, available) for mode in ("ask", "auto", "deny")
    }


def test_the_antigravity_row_carries_the_2026_09_22_forensics() -> None:
    """batch54 / AD-175：这一行从「整张表 declared」升到部分 live。

    真机（`agy_acp_server_1.1.1`）给了六位依据。守两件事：
    - 升上去的是**那五位布尔位**（`mode_semantics` 不是 QUIRK_BITS 里的一位，
      记不进 `verified_bits`，依据写在 notes 里）；
    - **没**升上去的两位仍是 declared——`tool_update_cumulative` 那一趟取不到，
      `needs_client_fs` 的证据只到「它没向我们要过文件」，而探针从没给它派过
      文件任务。填 `False` 是更谦逊的那一档，但谦逊不等于测过了。
    """
    preset = get_preset("antigravity")
    quirks = preset.quirks
    assert quirks.supports_session_resume is True
    assert quirks.supports_session_load is True
    assert quirks.supports_set_mode is True
    assert quirks.supports_set_model is True
    assert quirks.model_switch == "set_model"
    assert quirks.mode_semantics == "approval"
    assert quirks.thought_chunks is True
    assert quirks.needs_client_fs is False
    assert preset.verified_bits == frozenset(
        {
            "supports_session_resume",
            "supports_session_load",
            "supports_set_mode",
            "supports_set_model",
            "thought_chunks",
        }
    )
    # 这两位停在 declared：取不到 / 没派过文件任务，都不是「测出来如此」。
    assert "tool_update_cumulative" not in preset.verified_bits
    assert "needs_client_fs" not in preset.verified_bits
    assert preset.to_wire()["verifiedBits"]


def test_native_permission_names_are_not_confused_with_generic_policies() -> None:
    from drivers.acp.presets import get_preset

    assert get_preset("goose").approval_mode_ids == {"ask": "approve", "bypass": "auto"}


def test_in_process_bridge_uses_own_absolute_entrypoint() -> None:
    from pathlib import Path
    import sys

    preset = get_preset("crush")
    assert preset.command[0] == sys.executable
    assert Path(preset.command[1]).is_absolute()
    assert Path(preset.command[1]).name == "cli.py"
    assert preset.executable == "crush"
    assert preset.approval_option_id == "_approval"
    assert preset.approval_mode_ids == {"ask": "ask", "bypass": "bypass"}
    assert preset.elicitation_forms is True
    assert preset.quirks.mode_semantics == "none"
    assert preset.quirks.prefer_session_load is True
    assert preset.verified_bits == frozenset()


def test_process_defaults_do_not_leak_into_catalog_output() -> None:
    for name in ("opencode", "kilo"):
        preset = get_preset(name)
        assert preset.process_env == {"NODE_USE_SYSTEM_CA": "1"}
        assert "processEnv" not in preset.to_wire()
        assert "process_env" not in preset.to_wire()


def test_headless_bridge_exposes_only_supported_permission_policies() -> None:
    from drivers.acp.capabilities import resolve_conversation_mode_id

    preset = get_preset("commandcode")
    assert preset.executable == "command-code"
    assert preset.quirks.mcp_via_session_new is False
    assert preset.client_methods == {}
    assert preset.elicitation_forms is False
    assert preset.verified_bits == frozenset()
    modes = ("plan", "yolo")
    mapping = preset.approval_mode_ids
    assert resolve_conversation_mode_id("bypass", modes, mapping) == "yolo"
    assert resolve_conversation_mode_id("plan", modes, mapping) == "plan"
    for policy in ("ask", "auto", "read_only"):
        assert resolve_conversation_mode_id(policy, modes, mapping) is None
