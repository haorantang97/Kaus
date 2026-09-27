"""读引擎自己的配置：``<HERMES_HOME>/config.yaml`` → :class:`~drivers.base.EngineSettings`。

批次十三第 1 件。Media 项目的真机现象是：Kaus 库里这条 Binding 的
``default_model_id=null``、``runtime_config={}``，界面显示「未设置」，而这个
scope 自己的 ``config.yaml`` 里 model / 推理强度 / 审批档全都有值。两本账，
一本空的被当成了唯一的账。本模块把引擎那本账**读出来**（只读、不写、不合并），
让 `/api/bindings/{id}/effective-settings` 能如实回答「这个值是谁定的」。

批次十四：一本账其实是**两层**（继承链）
----------------------------------------
2026-09-03 真机复现：Media 绑定的 ``effective-settings`` 仍是 ``model: none``，
而用户在 Mac 上跑
``hermes -p media config get model`` 与 ``hermes config get model`` 都给出
``default: gpt-5.5 / provider: openai-codex``。原因是 **media profile 自己那份
``config.yaml`` 根本没有 ``model`` 键，Hermes 按继承回落到根配置
``~/.hermes/config.yaml``**——我们只读了 profile 那一层，于是读了个空。

所以本模块现在按 Hermes 的继承链**逐键回退**：

1. 先读 profile 层 ``<HERMES_HOME>/config.yaml``；
2. 该键缺（或值被判成「没有」）时，再看根层 ``<HERMES_ROOT>/config.yaml``；
3. 每个键记下它落在哪一层（:attr:`~drivers.base.EngineSettings.key_sources`），
   :attr:`~drivers.base.EngineSettings.source_ref` 用
   ``profile:<路径>`` / ``root:<路径>`` 标明来源（两层都出过力就都列上）。

对上游而言 ``source`` 仍然是 ``engine``——继承链是引擎内部的事，不是第三本账。
默认 scope 的 ``HERMES_HOME`` 就是 ``HERMES_ROOT``，此时只有一层，不会重复读。

读哪几个键（键名取证）
----------------------
======================  ===================================================
``model``               顶层。本仓库 ``server.py`` 的 ``/api/agent/{name}/levers``
                        长期读写这个键（软继承键，见 ``capability_import.py`` 的
                        ``KeyClassification("model", …, soft=True)``）。值有两种
                        形态：**裸模型 id 字符串**，或一份模型条目映射
                        （``{"default": "<model_id>", "provider": …}``，即
                        ``model-options.json`` 的一条）——两种都认，取 ``default``。
                        映射形态里**同时取 ``provider``**（批次十四：目录兜底要靠它
                        知道这个模型归谁）；同一份映射里的 ``base_url`` 之流
                        （见 :data:`MODEL_ENTRY_NEVER_READ_KEYS`）一个字都不读。
``agent.reasoning_effort``  ``server.py`` 的 levers 端点读写的正是这个子键，
                        取值 ``none|minimal|low|medium|high|xhigh``，
                        缺省 ``medium``（``_REASONING_EFFORTS``）。
                        另见 ``AGENTS.md``：「推理强度属于 agent 运行配置」。
``approvals.mode``      取值 ``manual|smart|off``，映射表见
                        :mod:`drivers.hermes.approval_map`；``test_regressions.py``
                        断言过 ``_read_config(...)["approvals"]["mode"]``。
``providers``           顶层映射，**键 = provider 名**。只取键名，值一个字都不读
                        （``capability_import.py`` 把它标了 ``credential_bearing``，
                        实测该段含 ``api_key``）。
======================  ===================================================

**假设与其边界（必须写明）：** 本仓库的运行环境跑不了真的 ``hermes`` CLI，因此
上面四条的取证来源是**本仓库长期在真机上读写这些键的生产代码**（``server.py`` /
``capability_import.py`` / ``test_regressions.py``），而不是 ``hermes config --help``
的当场输出。它们与 AD-67 那次 ``hermes config get delegation`` 的取证是同一性质
的旁证 + 生产事实。**读不到、形状不认识的键一律当「没有」**，绝不猜第二个键名，
所以假设错了的后果只有「少读到一项」，不会是「读出一个错值」。

安全边界（非可协商，任务规格）
------------------------------
1. **只读 ``config.yaml`` 这一个文件**。``.env`` / ``credentials/`` / ``auth*``
   一律不打开——本模块里没有任何一条打开别的文件名的代码路径；
2. ``providers`` 只取**键名**，值不读、不落、不返回；
3. 任何标量值先过 :func:`drivers.hermes.redaction.redact`，被脱敏器改动过的值
   （= 疑似密钥：长随机串 / ``sk-`` 之流）**直接丢弃**并留一条 diagnostics，
   不进 :class:`~drivers.base.EngineSettings`、不进响应；
4. ``model`` 映射形态里**只取** ``default``/``provider`` 两类键；``base_url``
   等一律不读、不落、不返回（:data:`MODEL_ENTRY_NEVER_READ_KEYS` 是白名单外
   显式点名的黑名单，读代码的人一眼能确认）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from drivers.base import EngineSettings
from drivers.hermes import approval_map
from drivers.hermes.redaction import redact

#: 引擎配置文件名。**本模块只打开这一个名字**（安全边界第 1 条）。
CONFIG_FILE_NAME: str = "config.yaml"

#: ``agent`` 段里的推理强度子键。
REASONING_SECTION: str = "agent"
REASONING_KEY: str = "reasoning_effort"

#: 顶层的模型键与 provider 键。
MODEL_KEY: str = "model"
PROVIDERS_KEY: str = "providers"

#: 一份模型条目映射里，真正的模型 id 所在的键（``model-options.json`` 的形状）。
MODEL_ENTRY_ID_KEYS: tuple[str, ...] = ("default", "model_id", "id")

#: 模型条目映射里 provider 名所在的键。**只取名字**，值不是凭据也不做他用。
MODEL_ENTRY_PROVIDER_KEYS: tuple[str, ...] = ("provider", "provider_id")

#: 模型条目映射里**永远不读**的键。真机上 ``hermes config get model`` 会连
#: ``base_url`` 一起打印，它既是部署细节也可能带路径里的私有信息——本模块不读它，
#: 因此它不可能出现在任何响应里。这条常量存在的意义是让「不读」可被测试断言。
MODEL_ENTRY_NEVER_READ_KEYS: tuple[str, ...] = ("base_url", "api_key", "api_base", "key")

#: 继承链的两层层名（写进 ``key_sources`` / ``source_ref``）。
LAYER_PROFILE: str = "profile"
LAYER_ROOT: str = "root"

#: Hermes 的**通用推理档位**。取证：本仓库 ``server.py`` 的
#: ``_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}``
#: （levers 端点用它校验写入值），以及 ``AGENTS.md`` 的
#: 「推理强度属于 agent 运行配置，不属于模型条目：``agent.reasoning_effort``
#: 可取 ``none|minimal|low|medium|high|xhigh``」。
#:
#: 批次十四第 3 件用它兜底：静态目录（``model-options.json``）说不出某个模型的
#: 档位时，档位并不因此不存在——它是 **agent 级**配置，对这台引擎上任何模型都成立。
#: 此前返回空列表，前端按 AD-71 直接不渲染下拉，用户就「没有推理强度可选」了。
HERMES_REASONING_LEVELS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)


def config_path(hermes_home: Path | str) -> Path:
    """``<HERMES_HOME>/config.yaml``。只拼路径，不碰磁盘。"""
    return Path(hermes_home).expanduser() / CONFIG_FILE_NAME


def read_engine_settings(
    *,
    binding_id: str,
    hermes_home: Path | str,
    hermes_root: Path | str | None = None,
) -> EngineSettings:
    """按 Hermes 的继承链读引擎侧的有效设置（批次十四）。

    ``hermes_home`` 是这条 Binding 自己的 scope（``<root>/profiles/<name>``，
    默认 scope 就是 root 自己）；``hermes_root`` 是 ``~/.hermes`` 那一层，
    由调用方给（Driver 手上就有；``session_bootstrap.hermes_root_for(gateway)``
    是从一条 GatewayConfig 倒推它的那个函数）。**不给 = 只读 profile 那一层**，
    与批次十三的行为完全一致。

    逐键回退：profile 层有这个键就用 profile 的，缺了才看 root 的。两层各自的
    读取失败（缺文件 / 坏 YAML）都只是一条 diagnostics，不抛——调用方要的是
    「引擎那边有没有这一项」，一个异常会把「没有」变成「出错」。
    """
    profile_path = config_path(hermes_home)
    layers: list[tuple[str, Path]] = [(LAYER_PROFILE, profile_path)]
    if hermes_root is not None:
        root_path = config_path(hermes_root)
        # 默认 scope 的 home 就是 root：同一个文件不读两遍，也不假装有两层。
        if root_path != profile_path:
            layers.append((LAYER_ROOT, root_path))

    parsed: list[tuple[str, Path, EngineSettings]] = []
    for label, path in layers:
        raw, diagnostics = _load_yaml(path)
        parsed.append(
            (
                label,
                path,
                parse_config(
                    binding_id=binding_id,
                    raw=raw,
                    source_ref=str(path),
                    diagnostics=diagnostics,
                ),
            )
        )
    return merge_layers(binding_id=binding_id, layers=parsed)


def merge_layers(
    *,
    binding_id: str,
    layers: Sequence[tuple[str, Path | str, EngineSettings]],
) -> EngineSettings:
    """把继承链上各层的解析结果**逐键**合成一份（前面的层赢）。

    每个键记下它落在哪一层；``source_ref`` 只列**真出过力**的层，形如
    ``profile:/…/config.yaml`` 或 ``profile:/…+root:/…``。一项都没读到时退回
    第一层的路径——那才是「我们去看过、但那儿什么都没有」的诚实说法。
    """
    single = len(layers) < 2
    notes: list[str] = []
    for label, _path, settings in layers:
        for line in settings.diagnostics:
            notes.append(line if single else f"[{label}] {line}")

    key_sources: list[tuple[str, str]] = []
    contributors: list[str] = []

    def _pick(key: str, getter: Any) -> tuple[Any, str | None]:
        for label, _path, settings in layers:
            value = getter(settings)
            if value:
                key_sources.append((key, label))
                if label not in contributors:
                    contributors.append(label)
                return value, label
        return None, None

    model_id, model_layer = _pick(MODEL_KEY, lambda s: s.model_id)
    # provider 与 model 是同一个键上的两半，必须一起来自同一层——否则会拼出一个
    # 「A 层的模型 + B 层的 provider」这种现实中不存在的组合。
    model_provider = next(
        (s.model_provider_id for label, _p, s in layers if label == model_layer), None
    )
    reasoning, _ = _pick(f"{REASONING_SECTION}.{REASONING_KEY}", lambda s: s.reasoning_effort)
    approval, _ = _pick(approval_map.CONFIG_PATH, lambda s: s.approval_mode)
    providers, _ = _pick(PROVIDERS_KEY, lambda s: s.provider_ids)

    by_label = {label: str(path) for label, path, _s in layers}
    if contributors:
        # 按继承链的顺序列，不按哪个键先被解析出来的顺序——读的人要的是「链长什么样」。
        ordered = [label for label, _p, _s in layers if label in contributors]
        source_ref = "+".join(f"{label}:{by_label[label]}" for label in ordered)
    elif layers:
        source_ref = f"{layers[0][0]}:{by_label[layers[0][0]]}"
    else:  # pragma: no cover - 调用方总会给至少一层
        source_ref = None

    return EngineSettings(
        binding_id=binding_id,
        model_id=model_id,
        model_provider_id=model_provider,
        reasoning_effort=reasoning,
        approval_mode=approval,
        provider_ids=providers or (),
        source_ref=source_ref,
        key_sources=tuple(key_sources),
        diagnostics=tuple(notes),
    )


def parse_config(
    *,
    binding_id: str,
    raw: Any,
    source_ref: str | None = None,
    diagnostics: tuple[str, ...] = (),
) -> EngineSettings:
    """已经读进内存的一份配置 → :class:`~drivers.base.EngineSettings`（可脱离磁盘单测）。"""
    notes = list(diagnostics)
    if not isinstance(raw, Mapping):
        if raw is not None:
            notes.append("引擎配置不是映射，按「没有引擎侧设置」处理")
        return EngineSettings(
            binding_id=binding_id, source_ref=source_ref, diagnostics=tuple(notes)
        )

    model_raw = raw.get(MODEL_KEY)
    model_id = _clean_scalar(_model_id_of(model_raw), field=MODEL_KEY, notes=notes)
    model_provider = (
        _clean_scalar(
            _model_provider_of(model_raw), field=f"{MODEL_KEY}.provider", notes=notes
        )
        if model_id
        else None
    )

    agent = raw.get(REASONING_SECTION)
    reasoning = (
        _clean_scalar(
            agent.get(REASONING_KEY),
            field=f"{REASONING_SECTION}.{REASONING_KEY}",
            notes=notes,
        )
        if isinstance(agent, Mapping)
        else None
    )

    approvals = raw.get(approval_map.CONFIG_SECTION)
    native_mode = _approval_native(
        approvals.get(approval_map.CONFIG_KEY) if isinstance(approvals, Mapping) else None
    )
    approval_mode = approval_map.to_generic(native_mode)
    if native_mode is not None and approval_mode is None:
        notes.append(
            f"引擎配置的 `{approval_map.CONFIG_PATH}` 取值 {native_mode!r} 不在映射表里，已忽略"
        )

    return EngineSettings(
        binding_id=binding_id,
        model_id=model_id,
        model_provider_id=model_provider,
        reasoning_effort=reasoning,
        approval_mode=approval_mode,
        provider_ids=_provider_ids(raw.get(PROVIDERS_KEY)),
        source_ref=source_ref,
        diagnostics=tuple(notes),
    )


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _load_yaml(path: Path) -> tuple[Any, tuple[str, ...]]:
    """读 YAML。缺文件 / 坏文件 / 没装 pyyaml 都返回 ``(None, 一条说明)``。"""
    try:
        import yaml  # 延迟导入：没装 pyyaml 的宿主照样能用 Driver 的其余部分
    except ImportError:  # pragma: no cover - 取决于宿主环境
        return None, ("未安装 pyyaml，读不了引擎配置",)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # 「这个 scope 还没有配置文件」是常态，不是错误——一句话说清就行。
        return None, (f"读不到引擎配置文件：{path}",)
    try:
        return yaml.safe_load(text) or {}, ()
    except Exception:  # noqa: BLE001 - 坏 YAML 不该让端点 500
        return None, (f"引擎配置文件解析失败（YAML 语法错误）：{path}",)


def _approval_native(value: Any) -> str | None:
    """``approvals.mode`` 的原始值 → 引擎取值串。

    一个真实会咬人的细节：**YAML 1.1 把不带引号的 ``off`` 解析成布尔 ``False``**
    （``on`` / ``yes`` / ``no`` 同理）。``server.py`` 用 ``yaml.safe_dump`` 写这个键
    时会自动加引号，所以它写出来的文件没问题；但用户手写的 ``config.yaml`` 里
    ``mode: off`` 是完全正常的写法，读回来却是 ``False``。这里把布尔还原成
    ``off`` / ``on``——``on`` 不在映射表里，会照常走「取值不认识」的分支。
    """
    if isinstance(value, bool):
        return "off" if value is False else "on"
    return value if isinstance(value, str) else None


def _model_id_of(value: Any) -> Any:
    """``model`` 键的两种形态 → 模型 id。认不出来返回 ``None``。"""
    if isinstance(value, Mapping):
        for key in MODEL_ENTRY_ID_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return None
    return value


def _model_provider_of(value: Any) -> Any:
    """模型条目映射里的 provider **名字**。裸字符串形态没有 provider，返回 ``None``。

    这里只按 :data:`MODEL_ENTRY_PROVIDER_KEYS` 取键；``base_url`` 之流
    （:data:`MODEL_ENTRY_NEVER_READ_KEYS`）不在这份名单里，因此不会被读到。
    """
    if not isinstance(value, Mapping):
        return None
    for key in MODEL_ENTRY_PROVIDER_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _clean_scalar(value: Any, *, field: str, notes: list[str]) -> str | None:
    """标量值的统一出口：空串当没有，疑似密钥直接丢弃。

    「疑似密钥」的判据不自己再造一套：把值喂给本 Driver 的脱敏器，
    :func:`~drivers.hermes.redaction.redact` 改动过它 = 它长得像密钥
    （长随机串 / ``Bearer …`` / ``API_SERVER_KEY=…`` 之类）。
    """
    if not isinstance(value, str):
        if value is not None:
            notes.append(f"引擎配置的 `{field}` 不是字符串，已忽略")
        return None
    text = value.strip()
    if not text:
        return None  # Hermes 用空串表示「未设置」（同 delegation_map 的 empty_string_is_none）
    if str(redact(text)) != text:
        notes.append(f"引擎配置的 `{field}` 取值疑似密钥，已丢弃（不入库、不进响应）")
        return None
    return text


def _provider_ids(value: Any) -> tuple[str, ...]:
    """``providers`` → provider 名的有序去重元组。**只取键名，绝不读值。**"""
    names: list[str] = []
    if isinstance(value, Mapping):
        names = [str(key) for key in value]
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, str):
                names.append(entry)
            elif isinstance(entry, Mapping):
                # 列表形态时，provider 的名字只可能在 id/name 上；其余键一概不看。
                for key in ("id", "name", "provider"):
                    candidate = entry.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        names.append(candidate.strip())
                        break
    out: list[str] = []
    for name in names:
        cleaned = name.strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return tuple(out)


__all__ = [
    "CONFIG_FILE_NAME",
    "HERMES_REASONING_LEVELS",
    "LAYER_PROFILE",
    "LAYER_ROOT",
    "MODEL_ENTRY_ID_KEYS",
    "MODEL_ENTRY_NEVER_READ_KEYS",
    "MODEL_ENTRY_PROVIDER_KEYS",
    "MODEL_KEY",
    "PROVIDERS_KEY",
    "REASONING_KEY",
    "REASONING_SECTION",
    "config_path",
    "merge_layers",
    "parse_config",
    "read_engine_settings",
]
