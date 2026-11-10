"""Agent 绑定能力的判定与每条原生能力的指纹（2026-11-10，设计稿 capability-import 第 3、5 节）。

只读文件，不执行任何东西。三层判定里前两层在这里：

1. 确定的：Agent 程序自己维护的（命令装在应用包里、在 Agent 自己的运行时目录里，或来自
   Agent 应用自带的插件市场）；这个 Agent 对这一类能力没有关闭方式（``not_isolable``）。
2. 规则判断：技能名就是这个 Agent 自己（讲它自己）；技能内容大量调用这个 Agent 的专有
   工具（``agent_tools``，阈值见 :data:`TOOL_CALL_THRESHOLD`）。

各 Agent 的名字、专有工具、自带市场写在 ``agent_capability_traits.json``（约定数据，不是行为）。
「Agent 自己维护」不单独作为判定：一个 Agent 随包带来的领域技能仍可共用。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

TRAITS: dict[str, dict] = {key: value for key, value in json.loads(
    Path(__file__).with_name("agent_capability_traits.json").read_text(encoding="utf-8")).items()
    if not key.startswith("_")}
#: 2026-11-10 在一个 Agent 的 44 个自带技能上核对：调用它专有工具 ≥10 次的恰好是讲它自己或
#: 调度别的 Agent 的 4 个（43、24、15、11 次），其余 ≤8 次。
TOOL_CALL_THRESHOLD = 10
MAX_SKILL_BYTES = 512 * 1024
NATIVE_KINDS = ("skill", "mcp", "plugin")


def kind_of(record: Mapping[str, Any]) -> str:
    return str(record.get("kind") or "skill")


def _skill_text(path: Path) -> str:
    folder = path.parent if path.name == "SKILL.md" else path
    chunks, total = [], 0
    try:
        files = sorted(folder.rglob("*.md"))[:200]
    except OSError:
        return ""
    for file in files:
        try:
            data = file.read_bytes()[: MAX_SKILL_BYTES - total]
        except OSError:
            continue
        chunks.append(data.decode("utf-8", "ignore"))
        total += len(data)
        if total >= MAX_SKILL_BYTES:
            break
    return "\n".join(chunks)


def _tool_calls(text: str, names: Iterable[str]) -> int:
    names = [re.escape(n) for n in names if n]
    if not names:
        return 0
    return len(re.findall(r"\b(" + "|".join(names) + r")\s*\(", text))


def bound_reason(record: Mapping[str, Any], *, supported_kinds: Iterable[str] | None = None) -> str | None:
    """这条原生能力为什么只能给它自己的 Agent 用；可共用时返回 ``None``。"""
    agent = str(record.get("agentKey") or "")
    traits = TRAITS.get(agent, {})
    kind = kind_of(record)
    if supported_kinds is not None and kind not in set(supported_kinds):
        return "not_isolable"
    name = str(record.get("name") or "").lower()
    if kind == "mcp":
        command = str((record.get("server") or {}).get("command") or "")
        markers = (".app/Contents/", *traits.get("ownedPathMarkers", ()))
        if command and any(marker in command for marker in markers):
            return "agent_owned"
        return None
    if kind == "plugin":
        market = str(record.get("pluginRef") or "").rpartition("@")[2]
        if market and market in traits.get("ownedMarkets", ()):
            return "agent_owned"
        return None
    base = name.split(":")[-1]
    if any(self_name and (base == self_name or base.startswith(self_name + "-")) for self_name in traits.get("selfNames", ())):
        return "agent_self"
    source = record.get("sourcePath")
    if source and traits.get("toolNames"):
        if _tool_calls(_skill_text(Path(str(source))), traits["toolNames"]) >= TOOL_CALL_THRESHOLD:
            return "agent_tools"
    return None


def fingerprint(record: Mapping[str, Any]) -> str:
    """内容变了它就变：技能看 SKILL.md 与目录里各文件的大小，MCP 看定义摘要，插件看版本与清单。"""
    kind = kind_of(record)
    digest = hashlib.sha256(kind.encode())
    if kind == "mcp":
        digest.update(json.dumps(record.get("server") or {}, sort_keys=True).encode())
    elif kind == "plugin":
        digest.update(json.dumps([record.get("version"), record.get("pluginRef"), record.get("sourcePath")]).encode())
    else:
        source = Path(str(record.get("sourcePath") or ""))
        folder = source.parent if source.name == "SKILL.md" else source
        try:
            digest.update((folder / "SKILL.md").read_bytes())
            for file in sorted(p for p in folder.rglob("*") if p.is_file())[:2000]:
                digest.update(f"{file.relative_to(folder)}\0{file.stat().st_size}\n".encode())
        except OSError:
            digest.update(b"unreadable")
    return digest.hexdigest()[:24]


def annotate(records: list[dict], *, supported: Mapping[str, Iterable[str]] | None = None) -> list[dict]:
    """给原生能力记录加上 ``fingerprint`` 与（若绑定）``bound`` / ``boundReason``。就地修改并返回。"""
    for record in records:
        capability_type = str(record.get("capabilityType") or "")
        if ":" not in capability_type or record.get("generated"):
            continue  # Kaus 自己的能力（能力库插件、生成的技能）不归任何 Agent
        if kind_of(record) not in NATIVE_KINDS:
            continue
        record["fingerprint"] = fingerprint(record)
        # An Agent whose adapter is not loaded right now is not judged on isolation at all.
        kinds = None if supported is None else supported.get(str(record.get("agentKey")))
        reason = bound_reason(record, supported_kinds=kinds)
        if reason:
            record["bound"], record["boundReason"] = True, reason
    return records


def supported_kinds(adapter: Any) -> tuple[str, ...]:
    """这个 Agent 的适配器能按次挡掉哪几类原生能力。"""
    kinds = []
    if adapter is not None and callable(getattr(adapter, "project", None)):
        kinds.append("skill")
    if adapter is not None and callable(getattr(adapter, "exclude_native_mcp", None)):
        kinds.append("mcp")
    if adapter is not None and callable(getattr(adapter, "exclude_native_plugins", None)):
        kinds.append("plugin")
    return tuple(kinds)


__all__ = ["TOOL_CALL_THRESHOLD", "annotate", "bound_reason", "fingerprint", "kind_of", "supported_kinds"]
