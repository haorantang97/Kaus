#!/usr/bin/env python3
"""INDEX.md 生成器 —— 上下文索引层 Phase 1（纯确定性，无 LLM，仅标准库）。

设计文档: docs/context-index-architecture.md
产物: ~/.hermes/INDEX.md —— 全组织唯一一份「谁知道什么/什么在哪/什么任务找谁」的地图。
agent 通过宪法条款 [rule:context-index] 在跨域时按需读取，索引本体绝不注入 SOUL。

结构（仿 SOUL.md 宪法块）:
    受管块  MANAGED_BEGIN ... MANAGED_END   —— 本模块生成，每次重建整体替换
    手写覆盖区  受管块之后的一切            —— 永不触碰，用户放路由表覆盖/固定语义

安全红线: 绝不读取 .env / auth / credentials；记忆只统计条数与主题词，不引原文进索引。
可独立运行: python3 index_builder.py [HERMES_HOME]  → 重建一次并打印路径。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

MANAGED_BEGIN = "<!-- ⚑ HERMES INDEX · 由 dashboard 自动生成，勿手改本区块（底部手写覆盖区永不被触碰） -->"
MANAGED_END = "<!-- ⚑ END INDEX -->"
_CONST_END = "<!-- ⚖ END CONSTITUTION -->"
_DEFAULT_PERSONA_SIG = "You are Hermes Agent, an intelligent AI assistant created by Nous Research"
_DORMANT_DAYS = 30
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|api[_-]?key|token\s*[:=]|Bearer\s+\S+)", re.I)

_DEFAULT_OVERRIDE = """
## 手写覆盖区（此行之下生成器永不触碰）

### 任务路由表（初版，手写；与名录冲突时以本表为准）
| 任务特征 | 首选 | 备选 | 管道 |
|---|---|---|---|
| 产出对外内容/文案/脚本 | media | personal-website | call_agent |
| 交易/行情判断 | trading | — | call_agent |
| 网页抓取/自动化 | web-automation | scrapling | call_agent |
| 查已沉淀的知识 | — | — | vault_read |
| 知识库录入/整理 | llm-wiki | memoris | call_agent |
| 仪表盘故障/运维 | dashboard-ops | dashboard-project | call_agent |
| 读书/阅读计划 | reading | — | call_agent |
"""


# ---------------------------------------------------------------------------
# 数据采集（全部只读，任何单项失败都不击穿整体）
# ---------------------------------------------------------------------------

def _profile_dir(home: Path, name: str) -> Path:
    return home if name == "default" else home / "profiles" / name


def _load_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _all_profiles(home: Path) -> list[str]:
    names = ["default"]
    pd = home / "profiles"
    if pd.is_dir():
        for c in sorted(pd.iterdir()):
            if c.is_dir() and not c.name.startswith("_") and not c.name.startswith("."):
                names.append(c.name)
    return names


def _is_twin(home: Path, name: str) -> bool:
    if name == "default":
        return False
    mem = _profile_dir(home, name) / "memories"
    try:
        if not mem.is_symlink():
            return False
        if os.path.realpath(str(mem)) == os.path.realpath(str(home / "memories")):
            return True
        # 兜底：挂载/容器视角下 symlink 目标是宿主机绝对路径，realpath 对不上——按路径尾部判定
        return os.readlink(str(mem)).rstrip("/").endswith(".hermes/memories")
    except OSError:
        return False


def _persona_summary(home: Path, name: str) -> tuple[str, bool]:
    """返回 (persona 首段摘要 ≤120 字符, 是否默认模板/空壳)。"""
    soul = _profile_dir(home, name) / "SOUL.md"
    try:
        txt = soul.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "", True
    body = txt.split(_CONST_END, 1)[-1] if _CONST_END in txt else txt
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)  # 整块剥离 HTML 注释，防注释正文混入摘要
    if _DEFAULT_PERSONA_SIG in body:
        return "", True
    lines = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("<!--") or s.startswith("-->") or s.startswith("#"):
            continue
        lines.append(s)
        if len(" ".join(lines)) > 100:
            break
    summary = " ".join(lines)[:120]
    summary = _SECRET_RE.sub("[…]", summary)
    return summary, not summary


def _memory_stats(home: Path, name: str) -> int:
    """MEMORY.md 的 § 条目数（不读原文进索引）。"""
    mem = _profile_dir(home, name) / "memories" / "MEMORY.md"
    try:
        txt = mem.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return 0
    if not txt:
        return 0
    return txt.count("§") + 1


def _last_session_ts(home: Path, name: str) -> float:
    db = _profile_dir(home, name) / "state.db"
    if not db.is_file():
        return 0.0
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            row = c.execute("select max(started_at) from sessions").fetchone()
            return float(row[0] or 0.0)
        finally:
            c.close()
    except Exception:
        return 0.0


def _status_of(home: Path, name: str, shell: bool, mem_count: int) -> str:
    if shell and mem_count == 0:
        return "shell"
    ts = _last_session_ts(home, name)
    if ts and (time.time() - ts) < _DORMANT_DAYS * 86400:
        return "active"
    return "dormant"


def _vault_map(home: Path) -> list[str]:
    cfg = _load_json(home / "dashboard" / "vault_config.json")
    root = Path(cfg.get("root", "")) if cfg.get("root") else None
    if not root or not root.is_dir():
        return []
    out = []
    try:
        for c in sorted(root.iterdir()):
            if c.is_dir() and not c.name.startswith("."):
                out.append(c.name)
    except OSError:
        pass
    return out[:30]


# ---------------------------------------------------------------------------
# 渲染 / 写入
# ---------------------------------------------------------------------------

def build_managed_block(home: Path) -> str:
    hierarchy = _load_json(home / "dashboard" / "hierarchy.json")
    labels = _load_json(home / "dashboard" / "labels.json")
    cards, shells = [], []
    for name in _all_profiles(home):
        label = labels.get(name, name)
        twin = _is_twin(home, name)
        persona, is_default = _persona_summary(home, name)
        mem_n = _memory_stats(home, name)
        if twin:
            cards.append(f"### {name}（{label}）\n- 状态: X 的分身（共享根记忆，只分角色）\n"
                         + (f"- 角色: {persona}\n" if persona else ""))
            continue
        status = _status_of(home, name, is_default, mem_n)
        if status == "shell":
            shells.append(f"{name}（{label}）")
            continue
        parent = hierarchy.get(name)
        lines = [f"### {name}（{label}）"]
        if parent:
            lines.append(f"- 上级: {parent}")
        lines.append(f"- 领域: {persona if persona else '（persona 未写——仅凭名字与记忆路由）'}")
        lines.append(f"- 记忆: {mem_n} 条")
        lines.append(f"- 状态: {status}")
        cards.append("\n".join(lines) + "\n")
    vault_dirs = _vault_map(home)
    parts = [
        MANAGED_BEGIN,
        "# Hermes 上下文索引",
        f"> 生成: {time.strftime('%Y-%m-%d %H:%M:%S')} · 由 index_builder 自动维护 · 跨域任务先读本文件再选管道",
        "",
        "## 使用规则（给 agent）",
        "1. 需要其他领域的**判断** → `call_agent(target=<id>, prompt=...)`（delegate MCP）；",
        "2. 需要已**沉淀的知识** → `vault_read`（vault MCP，全局共享库）；",
        "3. 目标状态为 shell/dormant → 不要强行路由，如实告知用户；",
        "4. 名录与底部手写路由表冲突时，以手写表为准。",
        "",
        "## Agent 名录",
        "",
        *cards,
    ]
    if shells:
        parts += ["### 空壳（persona 未写且无记忆——勿路由）", "- " + "、".join(shells), ""]
    if vault_dirs:
        parts += ["## Vault 地图（全局知识库顶层目录）", "- " + " · ".join(vault_dirs), ""]
    parts.append(MANAGED_END)
    return "\n".join(parts)


def write_index(home: Path) -> Path:
    """重建受管块，保留手写覆盖区；原子写。返回 INDEX.md 路径。"""
    home = Path(home)
    target = home / "INDEX.md"
    override = _DEFAULT_OVERRIDE
    if target.is_file():
        try:
            old = target.read_text(encoding="utf-8", errors="ignore")
            if MANAGED_END in old:
                tail = old.split(MANAGED_END, 1)[1]
                if tail.strip():
                    override = tail
        except OSError:
            pass
    content = build_managed_block(home) + "\n" + override.lstrip("\n")
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    return target


def signature(home: Path) -> tuple:
    """结构性脏检查签名：hierarchy/labels + 各 SOUL/MEMORY 的 mtime。5s tick 可承受（纯 stat）。"""
    home = Path(home)
    sig = []
    for p in (home / "dashboard" / "hierarchy.json", home / "dashboard" / "labels.json"):
        try:
            sig.append((str(p), p.stat().st_mtime_ns))
        except OSError:
            sig.append((str(p), 0))
    for name in _all_profiles(home):
        d = _profile_dir(home, name)
        for f in (d / "SOUL.md", d / "memories" / "MEMORY.md"):
            try:
                sig.append((name, f.name, f.stat().st_mtime_ns))
            except OSError:
                continue
    return tuple(sig)


if __name__ == "__main__":
    import sys
    home = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    print(write_index(home))
