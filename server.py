#!/usr/bin/env python3.11
"""
Hermes Agent — 多 Profile 仪表板后端
FastAPI + subprocess(hermes CLI) + 文件系统直读，端口 8877。

数据策略（务实 + 鲁棒）：
  - Profile 列表：优先解析 `hermes profile list`（定宽表格，◆ 标记当前 profile）；
    CLI 不可用时回退到直接扫描 ~/.hermes/profiles，确保仪表板永远能开。
  - 角色（源/继承/独立）：CLI 不暴露，由 skills/ 下的 symlink 图推导——被别人 symlink
    引用=源，自己 skills/ 里有指向别 profile 的 symlink=继承，二者皆非=独立。不依赖任何
    具体技能名（无 design-core 硬编码），契合"任意 agent 都能当源/子"的子母集架构。
  - 技能详情：直读 skills/**/SKILL.md 的 frontmatter（比解析 CLI 表格更可靠、更丰富）。
  - 最近会话：`hermes -p <name> sessions list --limit 8`（定宽表格，按表头切列）。
  - SOUL：直接读 SOUL.md。
  - 应用内对话：/ws/chat/{name} WebSocket + PTY 跑真·`hermes -p <name> chat`（前端 xterm.js）。
    就是 CLI 本体——原生多轮 / session / 工具 / 同一份配置；仪表板只做“显示器 + 键盘”。
    字节透传，不解析、不重造；hermes 升级也不会把它弄坏。

约定：所有路径用 os.path.expanduser / 绝对路径；HERMES_HOME 可用环境变量覆盖（便于测试）。
不 import hermes（无 Python SDK），只 subprocess + 直读文件。
"""
from __future__ import annotations

import asyncio
import atexit
import html
import copy
import fcntl
import hashlib
import json
import mimetypes
import os
import pty
import queue as queue_mod
import re
import select
import shlex
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# 路径 / 常量
# --------------------------------------------------------------------------- #
HERMES_HOME = Path(
    os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
).resolve()
PROFILES_DIR = HERMES_HOME / "profiles"
HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
INDEX_HTML = STATIC_DIR / "index.html"
MODEL_OPTIONS_FILE = HERE / "model-options.json"
DASHBOARD_CONFIG_FILE = HERE / "dashboard-config.json"
SESSION_ARCHIVE_FILE = HERE / "session-archive.json"
PTY_OUTPUT_DIR = HERMES_HOME / "outputs"
PTY_UPLOAD_DIR = PTY_OUTPUT_DIR / "uploads"
PTY_REPLAY_DIR = PTY_OUTPUT_DIR / "pty-replay"
_PTY_PREVIEW_ROOTS = (PTY_OUTPUT_DIR, Path("/tmp"), Path("/private/tmp"))
_PTY_PREVIEW_EXTS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".log",
}
_PTY_UPLOAD_EXTS = _PTY_PREVIEW_EXTS | {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip",
    ".heic", ".heif", ".mp3", ".m4a", ".wav", ".aac", ".mp4", ".mov", ".webm",
}
_PTY_TEXT_PREVIEW_EXTS = {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".log"}
_PTY_TEXT_PREVIEW_LIMIT = 2 * 1024 * 1024
_PTY_UPLOAD_LIMIT = 50 * 1024 * 1024

# hermes profile 名规则（与 hermes_cli/profiles.py 一致），用于防注入
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")  # 会话 ID 校验（防注入）
_PTY_RUNTIME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_MODEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,191}$")
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")             # 技能名校验（防 argv 注入）
_SESSION_HEALTH_TOP_N = 20
# Session Governor V2 is report-only and model-aware. V1 used fixed character
# limits, so a 272K/1M-token model could be labelled "critical" at only ~5-20%
# occupancy after one large tool result. V2 follows Hermes' own cheap pre-flight
# estimator (roughly chars/4) and grades estimated context against the model
# window recorded in model-options.json.
_SESSION_HEALTH_THRESHOLDS = {
    "critical": {"context_ratio": 0.85},
    "bloated": {"context_ratio": 0.70},
    "watch": {"context_ratio": 0.50},
}
_SESSION_HEALTH_FALLBACK_CONTEXT = 256_000
_SESSION_HEALTH_TOOL_HEAVY_RATIO = 0.25
_TERMINAL_LAB_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TERMINAL_TEXT_DEVICE_RESPONSE_RE = re.compile(
    r"(?:\x1b)?\](?:10|11|12);rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}(?:(?:\x1b)?\\|\x07)?|"
    r"\x1b\[[0-9]{1,3};[0-9]{1,3}R"
)
_TERMINAL_CONTROL_BYTES_RE = re.compile(
    rb"\x1b\][^\x07]*(?:\x07|\x1b\\)|"
    rb"\x1b\[[0-?]*[ -/]*[@-~]|"
    rb"\x1b(?:[()][A-Z0-9]|[=>78DEHMNOZc])"
)
_HERMES_STATUS_REPLAY_TEXT_RE = re.compile(
    r"^\s*(?:[^\w\s]\s*)?[A-Za-z0-9._:+@-]+\s*[│|]\s*"
    r"(?:(?:ctx\s+)?(?:--|-?\d+|[0-9.]+[KMG]?/[0-9.]+[KMG]?|[0-9.]+[KMG]?/1M))\s*[│|]\s*"
    r"\[[^\]]{3,}\]\s*(?:--|-?\d{1,3}%)(?:\s*[│|]\s*[^│|]+){0,5}\s*$",
    re.IGNORECASE,
)
_HERMES_PROGRESS_HINT_TEXT_RE = re.compile(
    r"^(?:⚕\s*[❯>]\s*)?(?:msg=interrupt|/queue|/bg|/steer|Ctrl\+C cancel)"
    r"(?:\s*[·|]\s*(?:msg=interrupt|/queue|/bg|/steer|Ctrl\+C cancel))*$",
    re.IGNORECASE,
)
_HERMES_REFLECTING_TEXT_RE = re.compile(r"^(?:ಠ_ಠ\s*)?reflecting\.\.\.$", re.IGNORECASE)
_SEP_CHARS = set("─-— ")  # 表格分隔行允许出现的字符（含 U+2500）


def _pty_runtime_id_matches_profile(runtime_id: str, name: str) -> bool:
    return runtime_id == name or runtime_id.startswith(f"{name}:")


def _resolve_hermes_bin() -> str:
    found = shutil.which("hermes")
    if found:
        return found
    for cand in (
        "/opt/homebrew/bin/hermes",
        os.path.expanduser("~/.local/bin/hermes"),
        "/usr/local/bin/hermes",
    ):
        if os.path.exists(cand):
            return cand
    return "hermes"  # 兜底交给 PATH


HERMES_BIN = _resolve_hermes_bin()


def _strip_terminal_text_device_responses(value: str) -> str:
    return _TERMINAL_TEXT_DEVICE_RESPONSE_RE.sub("", value or "")


def _load_root_env() -> dict:
    """解析根 ~/.hermes/.env 为 dict（凭据 + 配置）。

    为什么需要：`hermes -p <name>` 会把 HERMES_HOME 切到 ~/.hermes/profiles/<name>，
    而该目录下没有 .env —— 于是 hermes 自带的 dotenv 加载器读不到只存在于根
    ~/.hermes/.env 的 DEEPSEEK_API_KEY，oneshot(-z) 会以
    "Provider 'deepseek' ... no API key" 退出。这里读根 .env 并注入到每次 hermes
    子进程，使应用内对话无论目标哪个 profile 都能用（等同 hermes 对 default home 的行为）。
    """
    result: dict[str, str] = {}
    env_path = HERMES_HOME / ".env"
    try:
        text = env_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return result
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        result[key] = val
    return result


_ROOT_ENV = _load_root_env()


def run_hermes(args, timeout: int = 20, input_text: str | None = None):
    """调用 hermes CLI，返回 (returncode, stdout, stderr)。永不抛异常。
    input_text 可喂 stdin（如 uninstall 的确认 'y\\n'）。"""
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CLICOLOR", "0")
    # 注入根 .env 凭据：-p <profile> 子进程的 HERMES_HOME 下没有 .env，
    # 否则读不到 DEEPSEEK_API_KEY。已在真实环境中设置的变量不覆盖。
    for _k, _v in _ROOT_ENV.items():
        env.setdefault(_k, _v)
    try:
        proc = subprocess.run(
            [HERMES_BIN, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            input=input_text,
        )
        return proc.returncode, _ANSI_RE.sub("", proc.stdout or ""), (proc.stderr or "")
    except FileNotFoundError:
        return 127, "", "hermes 未找到"
    except subprocess.TimeoutExpired:
        return 124, "", "hermes 调用超时"
    except Exception as exc:  # noqa: BLE001 - 永不让 CLI 故障击穿接口
        return 1, "", str(exc)


_PROFILE_LIST_CACHE_TTL = 60.0
_PROFILE_LIST_CACHE_LOCK = threading.RLock()
_PROFILE_LIST_CACHE: dict = {"ts": 0.0, "rows": None, "source_error": None}


def _invalidate_profile_list_cache() -> None:
    """Profile 列表缓存只包住昂贵的 `hermes profile list`。
    config/model/skills 仍在 build_profiles() 内直读文件，避免软继承或技能分配显示陈旧。
    """
    with _PROFILE_LIST_CACHE_LOCK:
        _PROFILE_LIST_CACHE.update({"ts": 0.0, "rows": None, "source_error": None})


def _cached_profile_list_from_cli() -> tuple[list | None, str | None]:
    now = time.monotonic()
    with _PROFILE_LIST_CACHE_LOCK:
        age = now - float(_PROFILE_LIST_CACHE.get("ts") or 0.0)
        if 0 <= age < _PROFILE_LIST_CACHE_TTL:
            rows = _PROFILE_LIST_CACHE.get("rows")
            return copy.deepcopy(rows) if rows else None, _PROFILE_LIST_CACHE.get("source_error")

    rc, out, err = run_hermes(["profile", "list"])
    rows = parse_profile_list(out) if rc == 0 else None
    source_error = None
    if not rows:
        if rc != 0:
            source_error = (err or out or f"hermes 退出码 {rc}").strip()[:200]
        else:
            source_error = "hermes profile list 输出无法解析（格式可能已变，见 §8 对齐解析器）"

    with _PROFILE_LIST_CACHE_LOCK:
        _PROFILE_LIST_CACHE.update({
            "ts": now,
            "rows": copy.deepcopy(rows) if rows else None,
            "source_error": source_error,
        })
    return copy.deepcopy(rows) if rows else None, source_error


# --------------------------------------------------------------------------- #
# 文件系统直读
# --------------------------------------------------------------------------- #
def _profile_dir(name: str) -> Path:
    """default profile 就是 ~/.hermes 本身；其余在 profiles/<name>。"""
    return HERMES_HOME if name == "default" else PROFILES_DIR / name


def _all_profile_names() -> list:
    """磁盘上所有 profile 名（含 default）。"""
    names = []
    if HERMES_HOME.is_dir():
        names.append("default")
    if PROFILES_DIR.is_dir():
        try:
            for entry in sorted(PROFILES_DIR.iterdir()):
                if entry.is_dir() and _PROFILE_ID_RE.match(entry.name):
                    names.append(entry.name)
        except OSError:
            pass
    return names


def symlink_providers() -> set:
    """充当『共享源』的 profile 集合：被别的 profile 通过技能 symlink 引用的那些。
       一次扫全量，供 build_profiles 复用，避免每个 agent 重复全盘扫描。"""
    providers = set()
    for nm in _all_profile_names():
        for sl in skill_symlinks(nm):
            tp = sl.get("target_profile")
            if tp:
                providers.add(tp)
    return providers


def agent_role(name: str, providers: set = None) -> str:
    """角色由 symlink 图推导，不再依赖任何魔法技能名（去掉 design-core 硬编码）：
       source = 有别的 profile symlink 了它的技能（它是共享源）；
       shared = 它的 skills/ 里有指向别 profile 的 symlink（它在继承别人）；
       none   = 二者皆非（独立）。
       既是源又继承时优先记为 source——它在网络里是被依赖的一方。"""
    if providers is None:
        providers = symlink_providers()
    if name in providers:
        return "source"
    for sl in skill_symlinks(name):
        if sl.get("target_profile"):
            return "shared"
    return "none"


def _iter_skill_mds(sdir: Path):
    """枚举 skills/ 下所有 SKILL.md，并跟进顶层 symlink 目录（py3.11 的 rglob 不跟
       symlink，共享/继承来的技能目录因此会被漏掉，这里显式跟进）。
       产出 (md_path, category, via_symlink)。"""
    if not sdir.is_dir():
        return
    try:
        entries = sorted(sdir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():  # 含跟进 symlink 指向的目录
            is_link = entry.is_symlink()
            try:
                mds = sorted(entry.rglob("SKILL.md"))
            except OSError:
                mds = []
            for md in mds:
                s = str(md)
                if "/.hub/" in s or "/.git/" in s or "/.archive/" in s:   # .archive = curator 归档区
                    continue
                yield md, entry.name, is_link
        elif entry.name == "SKILL.md":
            yield entry, "(root)", False


def _skill_search_roots(name: str) -> list:
    """该 profile 实际加载技能的所有目录：自身 skills/ + config.yaml 的 skills.external_dirs。
       external_dirs 是 hermes 原生的"从额外目录加载技能"——分身/共享技能库就靠它活继承。"""
    roots = [_profile_dir(name) / "skills"]
    try:
        cfg = _read_config(name)
        ext = ((cfg.get("skills") or {}).get("external_dirs")) or []
        for p in ext:
            if isinstance(p, str) and p.strip():
                roots.append(Path(os.path.expanduser(p)))
    except Exception:  # noqa: BLE001 - 读 config 失败不应击穿技能统计
        pass
    return roots


def count_skills(name: str, cache: dict | None = None) -> int:
    """统计 SKILL.md 数量（排除 .hub/.git，跟进 symlink 共享层 + external_dirs 继承；按 realpath 去重）。"""
    roots = _skill_search_roots(name)
    if cache is not None:
        key = ("count", tuple(os.path.realpath(str(root)) for root in roots))
        if key in cache:
            return cache[key]
        root_sets = cache.setdefault("__root_sets__", {})
        seen = set()
        for root in roots:
            root_key = os.path.realpath(str(root))
            if root_key not in root_sets:
                root_seen = set()
                for md, _cat, _link in _iter_skill_mds(root):
                    root_seen.add(os.path.realpath(str(md)))
                root_sets[root_key] = root_seen
            seen.update(root_sets[root_key])
        count = len(seen)
        cache[key] = count
        return count
    seen = set()
    for root in roots:
        for md, _cat, _link in _iter_skill_mds(root):
            seen.add(os.path.realpath(str(md)))
    return len(seen)


def read_config_model(name: str):
    """轻量解析 config.yaml 的 model.default / model.provider，不依赖 pyyaml。"""
    cfg = _profile_dir(name) / "config.yaml"
    if not cfg.is_file():
        return None, None
    try:
        text = cfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None

    model = provider = None
    in_model_block = False
    for line in text.splitlines():
        # 单行写法： model: deepseek-v4-pro
        m1 = re.match(r"^model:\s*(\S.*?)\s*$", line)
        if m1 and not in_model_block:
            model = m1.group(1).strip().strip("\"'")
            continue
        # 块写法： model:\n  default: ...\n  provider: ...
        if re.match(r"^model:\s*$", line):
            in_model_block = True
            continue
        if in_model_block:
            if re.match(r"^\S", line):  # 回到顶层键，model 块结束
                break
            md = re.match(r"\s+default:\s*(.+?)\s*$", line)
            if md:
                model = md.group(1).strip().strip("\"'")
            mp = re.match(r"\s+provider:\s*(.+?)\s*$", line)
            if mp:
                provider = mp.group(1).strip().strip("\"'")
    return model, provider


def parse_frontmatter(text: str):
    """从 SKILL.md 的 YAML frontmatter 提取 (name, description)，支持块标量。"""
    if not text.startswith("---"):
        return None, ""
    end = text.find("\n---", 3)
    fm = text[3:end] if end != -1 else text[3:]
    lines = fm.splitlines()
    name = None
    desc = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        mn = re.match(r"^name:\s*(.*)$", line)
        if mn:
            name = mn.group(1).strip().strip("\"'")
            i += 1
            continue
        md = re.match(r"^description:\s*(.*)$", line)
        if md:
            val = md.group(1).strip()
            if val in ("", "|", "|-", "|+", ">", ">-", ">+"):  # 块标量
                block = []
                i += 1
                while i < len(lines) and (
                    lines[i].startswith((" ", "\t")) or lines[i].strip() == ""
                ):
                    if lines[i].strip() == "" and block:
                        break
                    if lines[i].strip():
                        block.append(lines[i].strip())
                    i += 1
                desc = " ".join(block)
            else:
                desc = val.strip("\"'")
                i += 1
            continue
        i += 1
    return name, desc


def _root_owner(root: Path) -> str:
    """一个 skills 目录属于哪个 profile（用于标注 external_dirs 继承的来源）。"""
    rp = os.path.realpath(str(root))
    for x in _all_profile_names():
        if rp == os.path.realpath(str(_profile_dir(x) / "skills")):
            return x
    return os.path.basename(os.path.dirname(rp)) or "external"


def profile_skills(name: str) -> dict:
    """按 skills/ 下顶层目录分组返回技能，并标注每条是自有还是继承（symlink 共享 / external_dirs）。
       去重（按 realpath）。inherited + source 让仪表盘正确显示"继承←源"。"""
    groups: dict[str, list] = {}
    seen = set()
    own_root = os.path.realpath(str(_profile_dir(name) / "skills"))
    sym = {s["name"]: s["target_profile"] for s in skill_symlinks(name)}  # 顶层 symlink 类目→源
    for root in _skill_search_roots(name):
        is_own = os.path.realpath(str(root)) == own_root
        ext_owner = None if is_own else _root_owner(root)
        for md, category, _is_link in _iter_skill_mds(root):
            rp = os.path.realpath(str(md))
            if rp in seen:
                continue
            seen.add(rp)
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            fm_name, desc = parse_frontmatter(text)
            if is_own:
                if category in sym:                        # 自己 skills/ 里这个类目是 symlink → 继承
                    inherited, source, via = True, sym[category], "symlink"
                else:
                    inherited, source, via = False, None, "own"   # 真·自有
            else:
                inherited, source, via = True, ext_owner, "external_dirs"  # 沿树继承
            groups.setdefault(category, []).append(
                {
                    "name": fm_name or md.parent.name,
                    "desc": (desc or "")[:200],
                    "inherited": inherited,
                    "source": source,
                    "via": via,
                }
            )
    return groups


# --------------------------------------------------------------------------- #
# CLI 表格解析
# --------------------------------------------------------------------------- #
def parse_profile_list(out: str):
    """解析 `hermes profile list`。列：Profile / Model / Gateway / Alias / Distribution。
    ◆ 标记当前 profile；各字段值均不含空格，故去掉 ◆ 后按空白切分即可。"""
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        if "Profile" in line and "Model" in line and "Gateway" in line:
            continue  # 表头
        if set(line.strip()) <= _SEP_CHARS:
            continue  # 分隔线
        active = "◆" in line
        parts = line.replace("◆", " ").split()
        if not parts:
            continue
        pname = parts[0]
        if pname != "default" and not _PROFILE_ID_RE.match(pname):
            continue
        model = parts[1] if len(parts) > 1 else "—"
        gateway = parts[2] if len(parts) > 2 else "—"
        alias = parts[3] if len(parts) > 3 else "—"
        dist = parts[4] if len(parts) > 4 else "—"
        rows.append(
            {
                "name": pname,
                "model": None if model == "—" else model,
                "gateway": None if gateway == "—" else gateway,
                "alias": None if alias == "—" else alias,
                "distribution": None if dist == "—" else dist,
                "active": active,
                "is_default": pname == "default",
            }
        )
    return rows or None


def parse_sessions(out: str):
    """解析 `hermes sessions list`。两种表头（含/不含 Title）。
    定宽列：按表头里各列名的起始位置切片，兼容 preview/时间里含空格的情况。"""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines or any("No sessions" in ln for ln in lines):
        return []

    hdr_idx = None
    for i, ln in enumerate(lines):
        if "ID" in ln and ("Preview" in ln or "Title" in ln):
            hdr_idx = i
            break
    if hdr_idx is None:
        return [{"raw": ln} for ln in lines[:8]]

    header = lines[hdr_idx]
    cols = []
    for key in ("Title", "Preview", "Last Active", "Src", "ID"):
        pos = header.find(key)
        if pos != -1:
            cols.append((key, pos))
    cols.sort(key=lambda kv: kv[1])

    sessions = []
    for ln in lines[hdr_idx + 1:]:
        if set(ln.strip()) <= _SEP_CHARS:
            continue
        rec = {}
        for j, (key, start) in enumerate(cols):
            stop = cols[j + 1][1] if j + 1 < len(cols) else len(ln)
            field = key.lower().replace(" ", "_")
            value = ln[start:stop].strip()
            if field in {"title", "preview"}:
                value = _strip_terminal_text_device_responses(value).strip()
            rec[field] = value
        if any(rec.values()):
            sessions.append(rec)
        if len(sessions) >= 8:
            break
    return sessions


def _relative_time_label(ts: float | None) -> str:
    if not ts:
        return "—"
    ago = max(0, int(time.time() - ts))
    if ago < 60:
        return f"{ago}s ago"
    if ago < 3600:
        return f"{ago // 60}m ago"
    if ago < 86400:
        return f"{ago // 3600}h ago"
    return f"{ago // 86400}d ago"


def _session_preview(value: str | None, limit: int = 64) -> str:
    text = _strip_terminal_text_device_responses(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _load_session_archive() -> dict:
    try:
        data = json.loads(SESSION_ARCHIVE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"profiles": {}}
    if not isinstance(data, dict):
        return {"profiles": {}}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        data["profiles"] = {}
    return data


def _session_archive_for_profile(name: str) -> dict:
    try:
        profile_dir = _profile_dir(name).resolve()
        expected_root = HERMES_HOME.resolve() if name == "default" else PROFILES_DIR.resolve()
        profile_dir.relative_to(expected_root)
    except (OSError, ValueError):
        return {}
    profiles = _load_session_archive().get("profiles") or {}
    profile = profiles.get(name) if isinstance(profiles, dict) else None
    return profile if isinstance(profile, dict) else {}


def _session_archive_hidden_ids(name: str) -> set[str]:
    profile = _session_archive_for_profile(name)
    hidden: set[str] = set()
    for key in ("hidden_session_ids", "archived_session_ids"):
        values = profile.get(key) or []
        if isinstance(values, list):
            hidden.update(str(item) for item in values if isinstance(item, str) and _SESSION_ID_RE.match(item))
    for chain in profile.get("chains") or []:
        if not isinstance(chain, dict):
            continue
        head = str(chain.get("head") or "")
        for sid in chain.get("archived_session_ids") or []:
            if isinstance(sid, str) and sid != head and _SESSION_ID_RE.match(sid):
                hidden.add(sid)
    return hidden


def _session_archive_title_override(name: str, sid: str) -> str | None:
    profile = _session_archive_for_profile(name)
    canonical = profile.get("canonical_titles") or {}
    if isinstance(canonical, dict):
        title = canonical.get(sid)
        if isinstance(title, str) and title.strip():
            return _session_preview(title.strip(), 96)
    for chain in profile.get("chains") or []:
        if not isinstance(chain, dict) or chain.get("head") != sid:
            continue
        title = chain.get("title")
        if isinstance(title, str) and title.strip():
            return _session_preview(title.strip(), 96)
    return None


def sessions_from_state_db(name: str, limit: int = 50) -> list[dict]:
    """直接读取 Hermes state.db 的 session 索引。

    `hermes sessions list` 当前只返回最近/活跃会话；dashboard 需要的是同一
    agent 下全部窗口的稳定索引，否则后台 PTY 重连时会把旧 session 误标为
    “后台 PTY 会话”。这里只读元数据和预览，不修改状态库。
    """
    db = _profile_dir(name) / "state.db"
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT
                  s.id,
                  s.title,
                  s.started_at,
                  s.ended_at,
                  s.message_count,
                  COALESCE((
                    SELECT MAX(m.timestamp)
                    FROM messages m
                    WHERE m.session_id = s.id
                  ), s.started_at) AS last_ts,
                  (
                    SELECT m.content
                    FROM messages m
                    WHERE m.session_id = s.id
                      AND m.content IS NOT NULL
                      AND TRIM(m.content) != ''
                    ORDER BY m.timestamp DESC, m.id DESC
                    LIMIT 1
                  ) AS preview
                FROM sessions s
                ORDER BY last_ts DESC, s.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []

    out: list[dict] = []
    hidden_ids = _session_archive_hidden_ids(name)
    for row in rows:
        sid = str(row["id"] or "").strip()
        if not _SESSION_ID_RE.match(sid):
            continue
        if sid in hidden_ids:
            continue
        if int(row["message_count"] or 0) <= 0 and not (row["preview"] or "").strip():
            continue
        title = _session_archive_title_override(name, sid) or _session_preview(row["title"], 96) or "未命名对话"
        last_ts = float(row["last_ts"] or row["started_at"] or 0)
        out.append({
            "id": sid,
            "title": title,
            "preview": _session_preview(row["preview"]),
            "last_active": _relative_time_label(last_ts),
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "message_count": int(row["message_count"] or 0),
            "source": "state_db",
        })
    return out


def _state_db_has_resumable_session(name: str) -> bool:
    """只读 state.db 判断是否有真实历史会话。

    这里不能调用 `hermes sessions list`：当前 Hermes CLI 的该命令会刷新/生成
    session 索引，dashboard 高频轮询会话列表时会制造“幽灵窗口”。对话入口只需
    知道能否安全 `--continue`，state.db 已是足够且无副作用的来源。
    """
    return any(int(item.get("message_count") or 0) > 0 for item in sessions_from_state_db(name, limit=3))


def active_pty_sessions_for_profile(name: str) -> dict[str, dict]:
    """Return live PTY runtimes keyed by saved session id for one profile."""
    live: dict[str, dict] = {}
    prefix = f"{name}:"

    def add_live(runtime_id: str, *, buffer_bytes: int, idle_seconds: float, subscribers: int, carrier: str) -> None:
        sid = runtime_id[len(prefix):].strip()
        if not _SESSION_ID_RE.match(sid):
            return
        # `new:<nonce>` and `board:<...>` are frontend-owned temporary PTY ids.
        # They are intentionally not Hermes session ids yet. If we expose them
        # through /api/sessions as "runtime" sessions, the tab/whiteboard views
        # reopen them as ghost conversations and can multiply windows after each
        # refresh. They become discoverable only after adopt-session renames the
        # runtime to the real Hermes session id printed by the CLI.
        if sid.startswith("new:") or sid.startswith("board:") or sid == "__new__":
            return
        live[sid] = {
            "runtime_id": runtime_id,
            "runtime_recovered": True,
            "runtime_buffer_bytes": buffer_bytes,
            "runtime_idle_seconds": idle_seconds,
            "runtime_subscribers": subscribers,
            "runtime_carrier": carrier,
            "last_active": "active now" if subscribers else _relative_time_label(time.time() - idle_seconds),
        }

    for runtime_id, runtime in list(_PTY_RUNTIMES.items()):
        if not runtime_id.startswith(prefix):
            continue
        returncode = runtime.proc.poll()
        active = (not runtime.closed) and returncode is None
        if not active:
            continue
        idle_seconds = max(0.0, time.time() - max(
            runtime.created_at,
            runtime.last_attach_at,
            runtime.last_detach_at,
            runtime.last_output_at,
        ))
        add_live(
            runtime_id,
            buffer_bytes=len(runtime.buffer),
            idle_seconds=idle_seconds,
            subscribers=len(runtime.subscribers),
            carrier=getattr(runtime, "carrier", "direct"),
        )
    for runtime_id in _known_pty_replay_runtime_ids():
        if runtime_id in _PTY_RUNTIMES or not runtime_id.startswith(prefix):
            continue
        if _screen_session_exists(_screen_session_name(runtime_id)):
            try:
                idle_seconds = max(0.0, time.time() - _pty_replay_path(runtime_id).stat().st_mtime)
            except (OSError, ValueError):
                idle_seconds = 0.0
            add_live(
                runtime_id,
                buffer_bytes=len(_load_pty_replay(runtime_id)),
                idle_seconds=idle_seconds,
                subscribers=0,
                carrier="screen-detached",
            )
    return live


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def list_profiles_fs():
    """CLI 不可用时的回退：直接扫描文件系统。"""
    rows = []
    if HERMES_HOME.is_dir():
        model, _ = read_config_model("default")
        rows.append(
            {
                "name": "default",
                "model": model,
                "gateway": None,
                "alias": None,
                "distribution": None,
                "active": False,
                "is_default": True,
            }
        )
    if PROFILES_DIR.is_dir():
        for entry in sorted(PROFILES_DIR.iterdir()):
            if not entry.is_dir() or not _PROFILE_ID_RE.match(entry.name):
                continue
            model, _ = read_config_model(entry.name)
            rows.append(
                {
                    "name": entry.name,
                    "model": model,
                    "gateway": None,
                    "alias": None,
                    "distribution": None,
                    "active": False,
                    "is_default": False,
                }
            )
    return rows


def build_profiles():
    """优先 CLI，回退文件系统；统一补充 role / skill_count / model / soul。
    返回 (rows, source, source_error)：CLI 失败回退到文件系统时，source_error 如实
    说明原因（§6 忠实性——不再"另起一套逻辑还假装没事"）。"""
    rows, source_error = _cached_profile_list_from_cli()
    source = "cli"
    if not rows:
        rows = list_profiles_fs()
        source = "filesystem"
    providers = symlink_providers()  # 一次全盘扫描，下面每个 agent 复用
    skill_count_cache: dict = {}
    model_cache: dict = {}
    for r in rows:
        r["role"] = agent_role(r["name"], providers)
        r["skill_count"] = count_skills(r["name"], skill_count_cache)
        r["model"], r["provider"] = read_effective_config_model(r["name"], model_cache)
        r["soul"] = (_profile_dir(r["name"]) / "SOUL.md").is_file()
    return rows, source, source_error


# --------------------------------------------------------------------------- #
# Agent 组织树：层级（仪表盘元数据）+ 技能 symlink（磁盘上真实的共享关系）
#   hermes 本身没有"父子/层级"概念（profiles 是平铺的），所以组织树是仪表盘
#   叠加的一层：hierarchy.json 存 {child: parent}；首次未配置时用技能 symlink 反推
#   （谁 symlink 了别人的技能 = 谁的子）。磁盘上真正变的只有技能 symlink。
# --------------------------------------------------------------------------- #
HIERARCHY_FILE = HERE / "hierarchy.json"
# 草稿：通过顶栏「+ 新建空 agent」生成、还没拖到位置的"游离"profile。
# 区别于 main 树下的正式节点：草稿 effective_parent 显式为 None（不兜底归 default），
# 不参与 config 物化、宪法订阅、kanban 派单；仅占着 profile 目录等用户拖入。
DRAFTS_FILE = HERE / "drafts.json"


def _load_drafts() -> set:
    try:
        d = json.loads(DRAFTS_FILE.read_text(encoding="utf-8"))
        return set(d) if isinstance(d, list) else set()
    except (OSError, ValueError):
        return set()


# 显示名层：hermes profile ID 必须小写（硬约束），但仪表盘可显示自定义名（首字母大写 / default→X）。
# labels.json = {profile_id: 显示名}；UI 用显示名，底层 ID 不变。无映射则回退显示 ID。
LABELS_FILE = HERE / "labels.json"


def _load_labels() -> dict:
    try:
        d = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_labels(d: dict) -> None:
    try:
        LABELS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _set_label(name: str, display: str) -> None:
    """设/改一个 profile 的显示名。display 为空/等于 id 则移除映射（回退显示 id）。"""
    d = _load_labels()
    display = (display or "").strip()
    if display and display != name:
        d[name] = display
    else:
        d.pop(name, None)
    _save_labels(d)


def _label(name: str, labels: dict = None) -> str:
    labels = labels if labels is not None else _load_labels()
    return labels.get(name) or name


def _save_drafts(names: set) -> None:
    try:
        DRAFTS_FILE.write_text(json.dumps(sorted(names), ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_hierarchy() -> dict:
    try:
        data = json.loads(HIERARCHY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_hierarchy(data: dict) -> None:
    try:
        tmp = HIERARCHY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HIERARCHY_FILE)
    except OSError:
        pass


def _symlink_owner(link: Path):
    """一条技能 symlink 指向哪个 profile（realpath 落在 profiles/<name>/ 下）。"""
    try:
        real = Path(os.path.realpath(str(link)))
    except OSError:
        return None
    try:
        return real.relative_to(PROFILES_DIR).parts[0]
    except (ValueError, IndexError):
        try:
            real.relative_to(HERMES_HOME)
            return "default"
        except ValueError:
            return None


def skill_symlinks(name: str) -> list:
    """该 profile 的 skills/ 下所有 symlink 技能 + 指向的源 profile。"""
    out = []
    sdir = _profile_dir(name) / "skills"
    if not sdir.is_dir():
        return out
    try:
        for entry in sorted(sdir.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                owner = _symlink_owner(entry)
                out.append({"name": entry.name,
                            "target_profile": None if owner == name else owner})
    except OSError:
        pass
    return out


def infer_parent(name: str):
    """无显式配置时：从技能 symlink 反推父级（取指向别 profile 的那条的源）。"""
    for sl in skill_symlinks(name):
        tp = sl.get("target_profile")
        if tp and tp != name:
            return tp
    return None


MAIN_AGENT = "default"  # 主 agent / 树根（本体）。所有其他 agent 默认归它（"default 上位"）。


def effective_parent(name: str, hierarchy: dict = None):
    """组织树父级：
       0) 草稿状态：通过顶栏「+ 新建空 agent」创建、还没拖到位置 → 显式 None（不挂 default）。
          用户必须拖动落位才算正式加入组织树；落位时 /api/move 会清掉草稿标记。
       1) hierarchy.json 显式优先（显式置空=用户主动设为顶层）；
       2) 主 agent（default）是根，无父；
       3) 否则 symlink 推断（继承谁的技能=谁的子）；
       4) 都没有 → 兜底归主 agent default。
       第 4 条让散建、还没接链路的 agent 也落在主 agent 下成为一级，而不是游离的"独立"节点。"""
    if name in _load_drafts():
        return None
    h = hierarchy if hierarchy is not None else _load_hierarchy()
    if name in h:
        return h[name] or None
    if name == MAIN_AGENT:
        return None
    return infer_parent(name) or MAIN_AGENT


# --------------------------------------------------------------------------- #
# Kill switch：仪表盘级"停用"闸。hermes 无原生 enabled 概念，这是仪表盘维护的一层闸——
#   停用 = 拒绝经仪表盘开终端 / 派活，完全可逆，不删任何东西。级联：停用某节点 =
#   它及其（按 effective_parent 推出的）整棵子树都视为停用。绕开仪表盘裸跑 CLI 不受此约束。
# --------------------------------------------------------------------------- #
KILLED_FILE = HERE / "killed.json"


def _load_killed() -> set:
    try:
        data = json.loads(KILLED_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _save_killed(names: set) -> None:
    try:
        tmp = KILLED_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(names), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(KILLED_FILE)
    except OSError:
        pass


def effective_killed(name: str, killed: set = None, hierarchy: dict = None) -> bool:
    """自己被停用，或任一祖先被停用（沿 effective_parent 上溯，级联冻结子树）。"""
    ks = killed if killed is not None else _load_killed()
    if not ks:
        return False
    h = hierarchy if hierarchy is not None else _load_hierarchy()
    seen, cur = set(), name
    while cur and cur not in seen:
        if cur in ks:
            return True
        seen.add(cur)
        cur = effective_parent(cur, h)
    return False


def is_main_twin(name: str) -> bool:
    """主 agent 的分身：它的 memories/ 是指向主 agent(default) memories 的 symlink（共享灵魂记忆）。
       靠文件系统结构判定，不靠名字硬编码——任何把 memories symlink 到 default 的 profile 都算分身。"""
    if name == MAIN_AGENT:
        return False
    mem = _profile_dir(name) / "memories"
    try:
        if not mem.is_symlink():
            return False
        main_mem = _profile_dir(MAIN_AGENT) / "memories"
        return os.path.realpath(str(mem)) == os.path.realpath(str(main_mem))
    except OSError:
        return False


def _twin_mode(name: str) -> str:
    """分身的"模式"语义标 —— 从它自己的 SOUL.md 头部【…】+ 高/低权限提取(数据驱动,非硬编码名字)。
       让系统/UI 能据此理解分身职责(architect=配置模式·高权限 / steward=后台模式·低权限),
       而不是只把它当"X 的又一个孩子"。"""
    try:
        txt = (_profile_dir(name) / "SOUL.md").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = re.search(r"【(.+?)】", txt)
    mode = (m.group(1).strip() if m else "").replace("分身", "").strip(" ·")
    tier = "高权限" if "高权限" in txt else ("低权限" if "低权限" in txt else "")
    return " · ".join(x for x in (mode, tier) if x)


def build_tree() -> dict:
    """组装组织树：节点含 parent/children/in_network/role/symlinks/skill_count。
       草稿 agent（顶栏 + 新建未落位）单独走 drafts 字段，不进 nodes/roots——前端在侧边栏开独立分区显示。"""
    rows, source, _ = build_profiles()
    h = _load_hierarchy()
    ks = _load_killed()
    drafts = _load_drafts()
    labels = _load_labels()
    pinned = set(_load_pinned())
    by = {}
    drafts_info = []
    for r in rows:
        nm = r["name"]
        info = {
            "name": nm,
            "label": _label(nm, labels),
            "is_pinned": nm in pinned,
            "model": r.get("model"),
            "role": r.get("role"),
            "in_network": r.get("role") != "none",
            "gateway": r.get("gateway"),
            "skill_count": r.get("skill_count", 0),
            "symlinks": skill_symlinks(nm),
            "parent": effective_parent(nm, h),
            "killed": nm in ks,
            "effective_killed": effective_killed(nm, ks, h),
            "main_twin": is_main_twin(nm),
            "is_draft": nm in drafts,
            "children": [],
        }
        if nm in drafts:
            drafts_info.append(info)
        else:
            by[nm] = info
    roots = []
    for nm, node in by.items():
        p = node["parent"]
        if p and p in by and p != nm:
            by[p]["children"].append(nm)
        else:
            node["parent"] = None
            roots.append(nm)
    for node in by.values():
        node["children"].sort()
        # 分身不是"下级域",是"主 agent 的另一种模式"。结构上单列 twins(= main_twin 的孩子),
        #   children 仍保留全部(物化/看板/仓库/网络等遍历不受影响);渲染层据此把 twins 挂到 X 上、
        #   不当分叉子节点。固定顺序(非字母),并带 twin_mode 语义标(从各自 SOUL 提取,非硬编码名)。
        node["twins"] = [c for c in node["children"] if by.get(c, {}).get("main_twin")]
    for nm, node in by.items():
        if node.get("main_twin"):
            node["twin_mode"] = _twin_mode(nm)
    return {"nodes": by, "roots": sorted(roots), "drafts": sorted(drafts_info, key=lambda d: d["name"]), "source": source}


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
app = FastAPI(title="Hermes Profiles Dashboard", docs_url=None, redoc_url=None)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --------------------------------------------------------------------------- #
# 前端实时刷新事件流（正规版）：后端监听关键状态文件/技能/Profile 目录变动 → SSE 推给 UI。
#   为什么不用前端轮询：组织树、技能迁移、模型白名单等可以被别的 agent/CLI 改动；
#   页面必须知道“外部状态变了”，而不是依赖用户手动刷新。
#   安全边界：只采集 stat/mtime/size/文件名，不读取 .env 或任何凭据内容。
# --------------------------------------------------------------------------- #
_EVENT_SUBSCRIBERS: set[queue_mod.Queue] = set()
_EVENT_LOCK = threading.Lock()
_EVENT_LAST_SIGNATURE: dict | None = None

_DASHBOARD_EVENT_FILES = (
    "hierarchy.json",
    "labels.json",
    "pinned.json",
    "killed.json",
    "drafts.json",
    "skill-blocklist.json",
    "skill_inherit_off.json",
    "config_inherit_block.json",
    "constitution_state.json",
    "distribution.json",
    "model-options.json",
    "vault_config.json",
    ".update_watch.json",
)
_PROFILE_EVENT_FILES = (
    "config.yaml",
    ".dash_inherited.json",
    "SOUL.md",
    "config.yaml.dashbak",
    "config.yaml.whbak",
)


def _state_stat_token(path: Path):
    """mtime 指纹；不读文件内容，symlink 只读 link target 字符串。"""
    try:
        st = path.lstat()
    except OSError:
        return None
    kind = "dir" if path.is_dir() else ("link" if path.is_symlink() else "file")
    token = [kind, int(st.st_mtime_ns), int(st.st_size)]
    if path.is_symlink():
        try:
            token.append(os.readlink(path))
        except OSError:
            token.append("")
    return token


def _compact_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(HERMES_HOME))
    except (OSError, ValueError):
        try:
            return str(path.resolve().relative_to(HERE))
        except (OSError, ValueError):
            return str(path)


def _profile_memory_signature(profile_dir: Path, limit: int = 80) -> dict:
    mem = profile_dir / "memories"
    out = {"__dir__": _state_stat_token(mem)}
    if not mem.exists():
        return out
    try:
        items = sorted(mem.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    n = 0
    for item in items:
        if item.name.startswith(".") or item.name == ".env":
            continue
        if item.is_dir() and not item.is_symlink():
            continue
        if item.suffix.lower() not in {".md", ".json", ".yaml", ".yml", ".txt"}:
            continue
        out[item.name] = _state_stat_token(item)
        n += 1
        if n >= limit:
            out["__truncated__"] = True
            break
    return out


def _profile_skills_signature(profile_dir: Path, limit: int = 3000) -> dict:
    root = profile_dir / "skills"
    out = {"__dir__": _state_stat_token(root)}
    if not root.exists():
        return out
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    n = 0
    for entry in entries:
        if entry.name in {".git", ".hub", ".archive"}:
            continue
        # 顶层 entry 的 lstat 足以捕捉技能增删、symlink 迁移和 .usage.json 更新。
        out[entry.name] = _state_stat_token(entry)
        n += 1
        if entry.is_dir() and not entry.is_symlink():
            try:
                skill_files = sorted(entry.rglob("SKILL.md"))
            except OSError:
                skill_files = []
            for skill_file in skill_files:
                rel = skill_file.relative_to(root)
                if any(part in {".git", ".hub", ".archive"} for part in rel.parts):
                    continue
                out[str(rel)] = _state_stat_token(skill_file)
                n += 1
                if n >= limit:
                    out["__truncated__"] = True
                    return out
        if n >= limit:
            out["__truncated__"] = True
            return out
    return out


def _dashboard_state_signature() -> dict:
    """当前 dashboard 可见数据的轻量签名。用于 SSE 变更检测和 /api/state-version。"""
    state = {name: _state_stat_token(HERE / name) for name in _DASHBOARD_EVENT_FILES}
    names = _all_profile_names()
    profiles, profile_files, memories, skills = {"__names__": names}, {}, {}, {}
    for name in names:
        pdir = _profile_dir(name)
        profile_files[name] = {fn: _state_stat_token(pdir / fn) for fn in _PROFILE_EVENT_FILES}
        memories[name] = _profile_memory_signature(pdir)
        skills[name] = _profile_skills_signature(pdir)
    return {
        "state": state,
        "root_config": {"config.yaml": _state_stat_token(HERMES_HOME / "config.yaml")},
        "profiles": profiles,
        "profile_files": profile_files,
        "memories": memories,
        "skills": skills,
    }


def _dashboard_signature_hash(signature: dict) -> str:
    raw = json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def _changed_keys(before: dict, after: dict, section: str) -> list[str]:
    b = before.get(section) or {}
    a = after.get(section) or {}
    keys = sorted(set(b) | set(a))
    return [key for key in keys if b.get(key) != a.get(key)]


def _dashboard_event_from_signatures(before: dict | None, after: dict) -> dict:
    scopes: set[str] = set()
    changed: dict[str, list[str]] = {}
    if before is None:
        return {
            "type": "state_snapshot",
            "scopes": ["network", "profiles", "skills", "config", "warehouse", "kanban", "vault"],
            "changed": {},
            "version": _dashboard_signature_hash(after),
            "ts": int(time.time() * 1000),
        }

    for section in ("state", "root_config", "profiles", "profile_files", "memories", "skills"):
        keys = _changed_keys(before, after, section)
        if keys:
            changed[section] = keys

    for name in changed.get("state", []):
        if name in {"hierarchy.json", "labels.json", "pinned.json", "killed.json", "drafts.json"}:
            scopes.update({"network", "profiles"})
        elif name in {"skill-blocklist.json", "skill_inherit_off.json", "distribution.json"}:
            scopes.update({"network", "profiles", "skills", "warehouse"})
        elif name in {"model-options.json", "config_inherit_block.json", "constitution_state.json"}:
            scopes.update({"profiles", "config"})
        elif name == "vault_config.json":
            scopes.add("vault")
        elif name == ".update_watch.json":
            scopes.add("dashboard")

    if "root_config" in changed:
        scopes.update({"profiles", "config"})
    if "profiles" in changed:
        scopes.update({"network", "profiles", "skills", "config", "kanban"})
    if "profile_files" in changed:
        scopes.update({"network", "profiles", "config"})
    if "memories" in changed:
        scopes.update({"profiles", "memory"})
    if "skills" in changed:
        scopes.update({"network", "profiles", "skills", "warehouse"})

    return {
        "type": "state_changed",
        "scopes": sorted(scopes) or ["dashboard"],
        "changed": changed,
        "version": _dashboard_signature_hash(after),
        "ts": int(time.time() * 1000),
    }


def _broadcast_dashboard_event(event: dict) -> None:
    with _EVENT_LOCK:
        subscribers = list(_EVENT_SUBSCRIBERS)
    for q in subscribers:
        try:
            q.put_nowait(event)
        except queue_mod.Full:
            try:
                q.get_nowait()
            except queue_mod.Empty:
                pass
            try:
                q.put_nowait(event)
            except queue_mod.Full:
                pass


def _dashboard_event_watch_loop(interval: float = 1.5) -> None:
    global _EVENT_LAST_SIGNATURE
    try:
        _EVENT_LAST_SIGNATURE = _dashboard_state_signature()
    except Exception:  # noqa: BLE001
        _EVENT_LAST_SIGNATURE = None
    while True:
        time.sleep(interval)
        try:
            current = _dashboard_state_signature()
            if _EVENT_LAST_SIGNATURE != current:
                event = _dashboard_event_from_signatures(_EVENT_LAST_SIGNATURE, current)
                _EVENT_LAST_SIGNATURE = current
                changed = event.get("changed") or {}
                if "profiles" in changed:
                    _invalidate_profile_list_cache()
                _broadcast_dashboard_event(event)
        except Exception:  # noqa: BLE001 - 事件流不能击穿主服务
            pass


def _sse_payload(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.get("/api/state-version")
def api_state_version():
    sig = _dashboard_state_signature()
    return {
        "ok": True,
        "version": _dashboard_signature_hash(sig),
        "profile_count": len((sig.get("profiles") or {}).get("__names__") or []),
        "ts": int(time.time() * 1000),
    }


@app.get("/api/events")
def api_events():
    q: queue_mod.Queue = queue_mod.Queue(maxsize=64)
    with _EVENT_LOCK:
        _EVENT_SUBSCRIBERS.add(q)

    async def event_stream():
        try:
            yield _sse_payload(_dashboard_event_from_signatures(None, _dashboard_state_signature()))
            # 批次二十八：首帧之后推一段填充注释，隧道/代理才会把前面的字节真的
            # 转出去（注释行不进 EventSource 的 message 回调）。
            yield ": " + " " * 2048 + "\n\n"
            while True:
                try:
                    event = await asyncio.to_thread(q.get, True, 15)
                except queue_mod.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield _sse_payload(event)
        finally:
            with _EVENT_LOCK:
                _EVENT_SUBSCRIBERS.discard(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# 应用内对话：WebSocket + PTY 跑真·hermes chat（字节透传，前端 xterm.js）
# --------------------------------------------------------------------------- #
def _pty_read(fd: int) -> bytes:
    try:
        return os.read(fd, 4096)
    except OSError:
        return b""


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _has_resumable_session(name: str) -> bool:
    """该 profile 是否有可续接的会话。hermes chat --continue 在『无历史会话』时会打印
    'No previous CLI session found' 并 exit(1)，所以加 --continue 前必须先探一下，
    否则首次打开（还没会话）的终端会立刻退出。"""
    return _state_db_has_resumable_session(name)


def _chat_argv(name: str, *, resume_sid=None, fresh: bool = False) -> list:
    override = os.environ.get("HERMES_CHAT_CMD")  # 测试覆盖（如 "cat"）
    if override:
        return shlex.split(override)
    # Even for the root/X profile, Hermes CLI stores resumable sessions under
    # the explicit profile namespace selected by `-p default`.  Omitting `-p`
    # can point resume lookups at a different implicit namespace and produce:
    # "Session not found: <sid>" despite `hermes sessions list -p default`
    # showing the session.
    argv = [HERMES_BIN, "-p", name, "chat"]
    model = _effective_model_entry(name)
    if model and model.get("default"):
        argv += ["--model", model["default"]]
        if model.get("provider"):
            argv += ["--provider", model["provider"]]
    # 选会话：指定 sid 恢复 > 否则非 fresh 且有历史则续接最近一次 > 否则开新会话。
    # 技能不在此预载——hermes 渐进式披露，按需/模型自动触发；技能归属是 agent 级（分层架构）。
    if resume_sid and _SESSION_ID_RE.match(resume_sid):
        argv += ["--resume", resume_sid]
    elif not fresh and _has_resumable_session(name):
        argv.append("--continue")
    return argv


def _load_dashboard_config() -> dict:
    """读取 dashboard 自身配置（非 agent 配置）。"""
    try:
        return json.loads(DASHBOARD_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_dashboard_config(cfg: dict) -> None:
    DASHBOARD_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _dashboard_terminal_app() -> str:
    """返回当前的终端应用名，默认 cmux。"""
    cfg = _load_dashboard_config()
    app = (cfg.get("terminal_app") or "").strip()
    return app if app else "cmux"


def _dashboard_available_terminal_apps() -> list[dict]:
    cfg = _load_dashboard_config()
    apps = cfg.get("available_terminal_apps")
    if isinstance(apps, list) and apps:
        return apps
    return [
        {"name": "cmux", "label": "cmux", "is_gui": True},
        {"name": "Terminal", "label": "macOS Terminal", "is_gui": True},
        {"name": "Ghostty", "label": "Ghostty", "is_gui": True},
        {"name": "iTerm", "label": "iTerm2", "is_gui": True},
    ]


def _terminal_chat_argv(name: str, session_id: str | None = None) -> list[str]:
    """Build the real-terminal argv.

    A fresh profile may intentionally omit a physical ``model`` key because the
    dashboard resolves it through the organisation tree (soft inheritance).
    Native ``hermes -p`` cannot see that tree, so fresh terminal sessions must
    receive the effective model/provider explicitly.  Resumed sessions keep the
    model/provider recorded in their own session metadata.
    """
    argv = ["hermes", "-p", name, "chat"]
    if session_id:
        argv += ["--resume", session_id]
        return argv
    model = _effective_model_entry(name)
    if model and model.get("default"):
        argv += ["--model", str(model["default"])]
        if model.get("provider"):
            argv += ["--provider", str(model["provider"])]
    return argv


def _terminal_chat_command(name: str, session_id: str | None = None) -> str:
    quoted = " ".join(shlex.quote(part) for part in _terminal_chat_argv(name, session_id))
    return f"cd {shlex.quote(str(HERE))} && {quoted}"


def _terminal_chat_script(name: str, session_id: str | None = None) -> str:
    argv = _terminal_chat_argv(name, session_id)
    quoted = " ".join(shlex.quote(part) for part in argv)
    return (
        "#!/bin/zsh\n"
        "export PATH=\"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH\"\n"
        f"cd {shlex.quote(str(HERE))} || exit 1\n"
        f"exec {quoted}\n"
    )


def _open_macos_terminal(command_script: str, name: str, session_id: str | None = None) -> Path:
    if shutil.which("open") is None:
        raise HTTPException(501, "当前系统没有 macOS open 命令，请复制启动命令手动打开终端")
    launcher_dir = PTY_OUTPUT_DIR / "terminal-launchers"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    sid = session_id or "new"
    safe_name = re.sub(r"[^A-Za-z0-9_.:-]+", "-", f"{name}-{sid}")[:80].strip(".-") or "hermes-chat"
    script_path = launcher_dir / f"{safe_name}-{int(time.time() * 1000)}.command"
    script_path.write_text(command_script, encoding="utf-8")
    script_path.chmod(0o700)
    terminal_app = _dashboard_terminal_app()
    try:
        subprocess.run(["open", "-a", terminal_app, str(script_path)], check=True, capture_output=True, text=True, timeout=10)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()[:300]
        raise HTTPException(502, f"打开 {terminal_app} 失败：{detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, f"打开 {terminal_app} 超时，请复制启动命令手动打开") from exc
    return script_path


def _chat_env() -> dict:
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")  # 与真终端一致：放开 24 位色，hermes logo/主题不被降到 256 色
    env.setdefault("LANG", "en_US.UTF-8")
    # 与 Hermes 官方 dashboard PTY 路径保持一致：浏览器内嵌 xterm 需要主缓冲区
    # + 禁用 TUI mouse tracking。否则 TUI 会把滚轮当成输入事件，最终在 prompt
    # 里表现成 ArrowUp/ArrowDown 历史命令切换，而不是滚动聊天记录。
    env.setdefault("HERMES_TUI_DISABLE_MOUSE", "1")
    env.setdefault("HERMES_TUI_INLINE", "1")
    for k, v in _ROOT_ENV.items():  # 注入根 .env 凭据（同 run_hermes）
        env.setdefault(k, v)
    return env


def _ws_origin_ok(ws: WebSocket) -> bool:
    """CSWSH 防护：/ws/chat 会起一个带工具权限的真 agent，必须挡住跨站 WebSocket 劫持。
    浏览器对 WS 握手一定带 Origin；非浏览器客户端（curl / 测试 / 本机脚本）无 Origin——
    它们无法被受害者浏览器当跳板，放行。带 Origin 时只认同源 localhost:8877。"""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    host = ws.headers.get("host", "")
    allowed = {
        f"http://{host}",
        f"https://{host}",
        "http://localhost:8877",
        "http://127.0.0.1:8877",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    }
    return origin in allowed


_PTY_BUFFER_LIMIT = 8 * 1024 * 1024
_PTY_REPLAY_LIMIT = 4 * 1024 * 1024
_PTY_REPLAY_FILE_LIMIT = _PTY_BUFFER_LIMIT
_PTY_FALLBACK_ROWS = 30
_PTY_FALLBACK_COLS = 100
_PTY_RUNTIMES: dict[str, "_PtyRuntime"] = {}
_PTY_SCREEN_PREFIX = "hermes_dash_"
_PTY_DEVICE_RESPONSE_RE = re.compile(
    rb"(?:\x1b)?\](?:10|11|12);rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}(?:(?:\x1b)?\\|\x07)?|"
    rb"\x1b\[[0-9]{1,3};[0-9]{1,3}R"
)


def _strip_pty_device_responses(data: bytes) -> bytes:
    """Remove terminal device replies that should never become user-visible shell output."""
    return _PTY_DEVICE_RESPONSE_RE.sub(b"", data)


def _tail_pty_replay(data: bytes, limit: int = _PTY_REPLAY_LIMIT) -> bytes:
    """Return a bounded replay tail for browser reattach.

    The browser terminal relies on this snapshot to rebuild visible scrollback
    after refresh. Keep it much smaller than the runtime ring buffer so
    reattaching behaves like a terminal tail instead of blocking the UI while a
    large transcript is replayed.
    """
    if len(data) <= limit:
        return data
    tail = data[-limit:]
    newline = tail.find(b"\n")
    if newline != -1:
        tail = tail[newline + 1:]
    while tail and (tail[0] & 0xC0) == 0x80:
        tail = tail[1:]
    prefix = (
        f"\r\n[Hermes dashboard: replay truncated to recent {limit // 1024}KB; "
        "full transcript remains in Hermes sessions]\r\n"
    ).encode("utf-8")
    return prefix + tail


def _bounded_pty_tail(data: bytes, limit: int = _PTY_REPLAY_FILE_LIMIT) -> bytes:
    """Return a byte-safe tail for persisted PTY replay storage.

    Unlike `_tail_pty_replay`, this does not add a human-visible truncation
    marker. The marker belongs to browser replay, not the on-disk ring file.
    """
    if len(data) <= limit:
        return data
    tail = data[-limit:]
    newline = tail.find(b"\n")
    if newline != -1:
        tail = tail[newline + 1:]
    while tail and (tail[0] & 0xC0) == 0x80:
        tail = tail[1:]
    return tail


def _pty_replay_path(runtime_id: str) -> Path:
    if not _PTY_RUNTIME_ID_RE.match(runtime_id):
        raise ValueError("invalid PTY runtime id")
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", runtime_id).strip(".-")[:96] or "runtime"
    digest = hashlib.sha256(runtime_id.encode("utf-8")).hexdigest()[:16]
    return PTY_REPLAY_DIR / f"{safe}-{digest}.bin"


def _runtime_id_from_replay_path(path: Path) -> str | None:
    if path.suffix != ".bin":
        return None
    stem = path.stem
    safe, sep, digest = stem.rpartition("-")
    if not sep or not re.fullmatch(r"[0-9a-f]{16}", digest):
        return None
    if not _PTY_RUNTIME_ID_RE.match(safe):
        return None
    if hashlib.sha256(safe.encode("utf-8")).hexdigest()[:16] != digest:
        return None
    return safe


def _known_pty_replay_runtime_ids() -> list[str]:
    try:
        paths = sorted(PTY_REPLAY_DIR.glob("*.bin"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return []
    ids: list[str] = []
    for path in paths:
        runtime_id = _runtime_id_from_replay_path(path)
        if runtime_id:
            ids.append(runtime_id)
    return ids


def _load_pty_replay(runtime_id: str) -> bytes:
    """Load last persisted replay bytes for a runtime, if any.

    This is deliberately only a screen replay cache. It does not imply the old
    process survived a backend restart; reconnect will still create/attach a real
    PTY and the replay is just the recent terminal picture.
    """
    try:
        path = _pty_replay_path(runtime_id)
        raw = path.read_bytes()
    except (OSError, ValueError):
        return b""
    return _bounded_pty_tail(raw)


def _persist_pty_replay_append(runtime_id: str, data: bytes) -> None:
    if not data:
        return
    try:
        path = _pty_replay_path(runtime_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(data)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= _PTY_REPLAY_FILE_LIMIT * 2:
            return
        raw = _bounded_pty_tail(path.read_bytes())
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    except OSError:
        return


def _persist_pty_replay_replace(runtime_id: str, data: bytes) -> None:
    try:
        path = _pty_replay_path(runtime_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(_bounded_pty_tail(data))
        os.replace(tmp, path)
    except (OSError, ValueError):
        return


def _clear_pty_replay(runtime_id: str) -> None:
    try:
        _pty_replay_path(runtime_id).unlink()
    except (OSError, ValueError):
        return


def _pty_screen_bin() -> str | None:
    """Return the optional persistent PTY carrier binary.

    Direct subprocesses are faithful while the backend lives, but a backend
    restart kills them.  `screen` lets the backend own only an attach process,
    while the real Hermes TTY can survive server restarts and reconnects.
    """
    carrier = os.environ.get("HERMES_PTY_CARRIER", "screen").strip().lower()
    if carrier in {"", "0", "false", "off", "none", "direct"}:
        return None
    if os.environ.get("HERMES_CHAT_CMD"):
        return None
    return shutil.which("screen") or ("/usr/bin/screen" if Path("/usr/bin/screen").exists() else None)


def _screen_session_name(runtime_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", runtime_id).strip("._-") or "runtime"
    digest = hashlib.sha256(runtime_id.encode("utf-8")).hexdigest()[:12]
    return f"{_PTY_SCREEN_PREFIX}{safe[:48]}_{digest}"


def _screen_env(env: dict | None = None) -> dict:
    screen_env = dict(env or _chat_env())
    screen_env.setdefault("TERM", "xterm-256color")
    screen_env.setdefault("COLORTERM", "truecolor")
    return screen_env


def _screen_session_exists(session_name: str, screen_bin: str | None = None) -> bool:
    screen_bin = screen_bin or _pty_screen_bin()
    if not screen_bin:
        return False
    try:
        completed = subprocess.run(
            [screen_bin, "-ls"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2,
            env=_screen_env(os.environ),
            check=False,
        )
    except Exception:
        return False
    return f".{session_name}" in (completed.stdout or "")


def _quit_screen_session(session_name: str | None, screen_bin: str | None = None) -> bool:
    if not session_name:
        return False
    screen_bin = screen_bin or _pty_screen_bin()
    if not screen_bin:
        return False
    try:
        completed = subprocess.run(
            [screen_bin, "-S", session_name, "-X", "quit"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            env=_screen_env(os.environ),
            check=False,
        )
    except Exception:
        return False
    return completed.returncode == 0


def _rename_screen_session(old_name: str | None, new_name: str, screen_bin: str | None = None) -> bool:
    if not old_name or old_name == new_name:
        return True
    screen_bin = screen_bin or _pty_screen_bin()
    if not screen_bin:
        return False
    try:
        completed = subprocess.run(
            [screen_bin, "-S", old_name, "-X", "sessionname", new_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            env=_screen_env(os.environ),
            check=False,
        )
    except Exception:
        return False
    return completed.returncode == 0


# 设备响应可能被 os.read 边界切成两半——逐块剥离会漏掉跨块的半截序列，
# 残渣（"]11;rgb:…"、"\x1b[12;34R"）就会以乱码形式出现在终端里。
# 解决：每次读完检查末尾是否像"被切断的设备响应前缀"，是则暂存到下一块再剥。
_PTY_PARTIAL_RESPONSE_WINDOW = 64
_PTY_CSI_CURSOR_PREFIX_RE = re.compile(rb"[0-9]{0,3}(?:;[0-9]{0,3})?")
# 全可选嵌套：响应体的任意前缀（含"完整但终结符还没到"的形态，末尾可跟半个 ST 的 ESC）
_PTY_OSC_COLOR_PREFIX_RE = re.compile(
    rb"(?:1(?:[012](?:;(?:r(?:g(?:b(?::(?:[0-9a-fA-F]{1,4}"
    rb"(?:/(?:[0-9a-fA-F]{1,4}(?:/[0-9a-fA-F]{0,4})?)?)?)?)?)?)?)?)?)?)?\x1b?"
)


def _is_partial_device_response(segment: bytes) -> bool:
    """segment 是否可能是一个被读取边界切断的设备响应前缀。

    必须在剥离之前判断：无终结符的"完整"响应会被剥离器提前吃掉，
    下一块才到的终结符（\\x07 或 ESC\\）就会泄漏成可见字符。"""
    if not segment:
        return False
    head = segment[0:1]
    if head == b"\x1b":
        body = segment[1:]
        if not body:
            return True  # 裸 ESC 结尾：等下一块再判断
        if body[0:1] == b"[":
            return bool(_PTY_CSI_CURSOR_PREFIX_RE.fullmatch(body[1:]))
        if body[0:1] == b"]":
            return bool(_PTY_OSC_COLOR_PREFIX_RE.fullmatch(body[1:]))
        return False
    if head == b"]":
        return bool(_PTY_OSC_COLOR_PREFIX_RE.fullmatch(segment[1:]))
    return False


def _device_response_carry_len(data: bytes) -> int:
    """返回末尾需要暂存（等待下一块拼接后再剥离）的字节数。"""
    start = max(0, len(data) - _PTY_PARTIAL_RESPONSE_WINDOW)
    for offset in range(start, len(data)):
        if _is_partial_device_response(data[offset:]):
            return len(data) - offset
    return 0


# 终端能力查询：只在重连回放快照时剥离（活流里必须放行，TUI 在等真实终端应答）。
# OSC 10/11/12 颜色查询、DSR(6n/5n)、DA1/DA2、kitty 键盘(?u)、XTVERSION(>q)、
# DECRQM(?..$p)、XTGETTCAP(DCS +q)。
_PTY_REPLAY_QUERY_RE = re.compile(
    rb"\x1b\](?:10|11|12);\?(?:\x07|\x1b\\)|"
    rb"\x1b\[(?:6n|5n|0?c|>0?c|\?u|>0?q)|"
    rb"\x1b\[\?[0-9;]{1,8}\$p|"
    rb"\x1bP\+q[0-9a-fA-F;]*\x1b\\"
)
_PTY_REPLAY_SCREEN_RESET_RE = re.compile(
    rb"(?:\x1b\[(?:[0-9;]*[Hf]|[0-3]?J)|\x1b\[\?1049[hl])+"
)
_FULLSCREEN_EDITOR_REPLAY_MARKERS = (
    b"UW PICO",
    b"GNU nano",
    b"^G Get Help",
    b"^X Exit",
    b"WriteOut",
    b"Read File",
    b"Cut Text",
    b"UnCut Text",
)
_GENERIC_PROFILE_PROMPT_RE = re.compile(
    rb"(?:^|[\r\n])(?P<prompt>(?:[a-z0-9][a-z0-9_-]{0,63}\s*)?\xe2\x9d\xaf|"
    rb"[a-z0-9][a-z0-9_-]{0,63}\s*>)"
)
_PTY_CONTROL_FRAGMENT_RE = (
    rb"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|"
    rb"\x1b\[[0-?]*[ -/]*[@-~]|"
    rb"\x1b(?:[()][A-Z0-9]|[=>78DEHMNOZc]))*"
)
_PTY_GENERIC_PROMPT_ONLY_TEXT_RE = re.compile(
    r"^(?:(?:[a-z0-9][a-z0-9_-]{0,63}\s*)?[❯>]|[a-z0-9][a-z0-9_-]{0,63}\s*>)$",
    re.IGNORECASE,
)
_PTY_FRAME_SEPARATOR_TEXT_RE = re.compile(r"^[─━═=\\-—_]{12,}$")


def _is_hermes_status_replay_frame(data: bytes) -> bool:
    """Hermes CLI 的底部状态栏是动态 TUI 帧，不应作为历史文本回放。

    原生终端靠光标移动/覆盖把它显示成单行；网页重连时如果把原始控制序列
    当历史逐字重放，缺失当时的光标状态就会变成截图里的多条黑色状态块。
    这里只在“回放快照”阶段丢弃这类状态帧，活流仍保持真 PTY 透传。
    """
    if not data:
        return False
    text = _TERMINAL_CONTROL_BYTES_RE.sub(b"", data).decode("utf-8", errors="replace")
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    return bool(_HERMES_STATUS_REPLAY_TEXT_RE.match(text))


def _drop_hermes_status_replay_frames(data: bytes) -> bytes:
    if not data:
        return data
    parts = re.split(rb"(\r\n|\n|\r)", data)
    output: list[bytes] = []
    for idx in range(0, len(parts), 2):
        chunk = parts[idx]
        sep = parts[idx + 1] if idx + 1 < len(parts) else b""
        if _is_hermes_status_replay_frame(chunk):
            continue
        output.append(chunk)
        output.append(sep)
    return b"".join(output)


def _neutralize_replay_screen_resets(data: bytes) -> bytes:
    """Turn destructive full-screen redraw controls into transcript separators.

    Live PTY output must remain byte-faithful, but browser reattach writes a
    stored byte stream into a fresh xterm instance. If the replay contains
    ``CSI 2J``/cursor-home frames, xterm faithfully clears/overwrites its own
    scrollback and the user cannot scroll to earlier conversation. Native
    terminals keep a separate scrollback; replay snapshots need the same
    protection.
    """
    if not data:
        return data
    return _PTY_REPLAY_SCREEN_RESET_RE.sub(b"\r\n", data)


def _visible_terminal_line_text(chunk: bytes) -> str:
    visible = _TERMINAL_CONTROL_BYTES_RE.sub(b"", chunk or b"")
    text = visible.decode("utf-8", errors="replace")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text.strip()


def _is_hermes_progress_line(chunk: bytes) -> bool:
    text = _visible_terminal_line_text(chunk)
    return bool(
        text
        and (
            _HERMES_PROGRESS_HINT_TEXT_RE.fullmatch(text)
            or _HERMES_REFLECTING_TEXT_RE.fullmatch(text)
            or ("/queue" in text and "/bg" in text and "/steer" in text)
        )
    )


def _progress_replay_chunk_kind(chunk: bytes) -> str:
    if _is_hermes_progress_line(chunk):
        return "progress"
    if _is_prompt_frame_separator_chunk(chunk) or _is_prompt_frame_separator_fragment_chunk(chunk):
        return "separator"
    if _is_prompt_frame_blank_chunk(chunk):
        return "blank"
    return "content"


def _drop_hermes_progress_replay_frames(data: bytes) -> bytes:
    """Drop transient Hermes thinking/progress frames from stored PTY transcript.

    Hermes CLI may repaint progress rows such as ``ಠ_ಠ reflecting...`` with
    terminal cursor control. Native terminals show one changing status; replaying
    the raw buffer in a browser turns that into dozens of fake transcript lines.
    Keep real content, but discard progress-only runs and their separators.
    """
    if not data:
        return data

    parts = re.split(rb"(\r\n|\n|\r)", data)
    output: list[bytes] = []
    pending_frame: list[tuple[str, bytes]] = []
    dropping_progress = False

    def flush_pending_as_content() -> None:
        nonlocal pending_frame
        if pending_frame:
            output.extend(entry for _, entry in pending_frame)
            pending_frame = []

    for idx in range(0, len(parts), 2):
        chunk = parts[idx]
        sep = parts[idx + 1] if idx + 1 < len(parts) else b""
        entry = chunk + sep
        kind = _progress_replay_chunk_kind(chunk)
        if kind == "progress":
            pending_frame = []
            dropping_progress = True
            continue
        if kind in {"separator", "blank"}:
            if dropping_progress:
                continue
            pending_frame.append((kind, entry))
            continue
        dropping_progress = False
        flush_pending_as_content()
        output.append(entry)
    flush_pending_as_content()
    return b"".join(output)


def _profile_prompt_re(name: str | None) -> re.Pattern[bytes]:
    if name and _PROFILE_ID_RE.fullmatch(name):
        return re.compile(
            rb"(?:^|[\r\n])(?P<prompt>"
            + re.escape(name.encode("utf-8"))
            + rb"\s*(?:\xe2\x9d\xaf|>))"
        )
    return _GENERIC_PROFILE_PROMPT_RE


def _is_prompt_only_replay_chunk(chunk: bytes, name: str | None = None) -> bool:
    """Return True when a replay line is only the active shell prompt.

    Hermes/xterm may redraw the prompt during resize or reattach. Those redraws are
    terminal state, not transcript content; when they enter the replay ring buffer
    they show up as multiple fake input rows. Only prompt-only rows are collapsed:
    prompt+command lines remain intact.
    """
    if not chunk:
        return False
    visible = _TERMINAL_CONTROL_BYTES_RE.sub(b"", chunk)
    visible = re.sub(rb"[\x00-\x1f\x7f]", b"", visible)
    text = visible.decode("utf-8", errors="replace").strip()
    if not text:
        return False
    if name and _PROFILE_ID_RE.fullmatch(name):
        return bool(
            re.fullmatch(rf"(?:{re.escape(name)}\s*)?[❯>]|{re.escape(name)}\s*>", text, re.IGNORECASE)
        )
    return bool(_PTY_GENERIC_PROMPT_ONLY_TEXT_RE.fullmatch(text))


def _is_prompt_frame_separator_chunk(chunk: bytes) -> bool:
    if not chunk:
        return False
    visible = _TERMINAL_CONTROL_BYTES_RE.sub(b"", chunk)
    visible = re.sub(rb"[\x00-\x1f\x7f]", b"", visible)
    text = visible.decode("utf-8", errors="replace").strip()
    return bool(_PTY_FRAME_SEPARATOR_TEXT_RE.fullmatch(text))


def _is_prompt_frame_separator_fragment_chunk(chunk: bytes) -> bool:
    if not chunk:
        return False
    visible = _TERMINAL_CONTROL_BYTES_RE.sub(b"", chunk)
    visible = re.sub(rb"[\x00-\x1f\x7f]", b"", visible)
    text = visible.decode("utf-8", errors="replace").strip()
    return bool(re.fullmatch(r"[─━═=\-—_]+", text))


def _is_prompt_frame_blank_chunk(chunk: bytes) -> bool:
    visible = _TERMINAL_CONTROL_BYTES_RE.sub(b"", chunk or b"")
    visible = re.sub(rb"[\x00-\x1f\x7f]", b"", visible)
    return not visible.strip()


def _prompt_replay_chunk_kind(chunk: bytes, name: str | None = None) -> str:
    if _is_prompt_only_replay_chunk(chunk, name):
        return "prompt"
    if _is_prompt_frame_separator_chunk(chunk) or _is_prompt_frame_separator_fragment_chunk(chunk):
        return "separator"
    if _is_prompt_frame_blank_chunk(chunk):
        return "blank"
    return "content"


def _is_prompt_redraw_frame(data: bytes, name: str | None = None) -> bool:
    """True when a live chunk is only a prompt redraw frame.

    This is intentionally narrower than replay cleanup: a command line such as
    ``web-automation > run`` is content and must remain. Pure prompt redraws are
    terminal state, not user text, so consecutive frames must not be appended to
    the PTY buffer.
    """
    if not data:
        return False
    parts = re.split(rb"(\r\n|\n|\r)", data)
    saw_prompt = False
    for idx in range(0, len(parts), 2):
        kind = _prompt_replay_chunk_kind(parts[idx], name)
        if kind == "prompt":
            saw_prompt = True
        elif kind in {"separator", "blank"}:
            continue
        else:
            return False
    return saw_prompt


def _ends_with_prompt_redraw(data: bytes, name: str | None = None) -> bool:
    if not data:
        return False
    parts = re.split(rb"(\r\n|\n|\r)", data)
    last_meaningful = "blank"
    for idx in range(0, len(parts), 2):
        kind = _prompt_replay_chunk_kind(parts[idx], name)
        if kind in {"blank", "separator"}:
            continue
        last_meaningful = kind
    return last_meaningful == "prompt"


def _compact_inline_prompt_replay_runs(data: bytes, name: str | None = None) -> bytes:
    """Collapse same-line prompt redraw runs such as ``web >web > web >``.

    Line-based cleanup catches prompt rows separated by CR/LF. Some terminal
    redraws, however, are emitted without line separators; handle those as a
    narrow textual normalization while preserving non-prompt bytes.
    """
    control = _PTY_CONTROL_FRAGMENT_RE
    prompt_atom: bytes
    replacement: bytes
    if name and _PROFILE_ID_RE.fullmatch(name):
        encoded_name = re.escape(name.encode("utf-8"))
        prompt_atom = (
            control
            + rb"(?:"
            + encoded_name
            + control
            + rb"[ \t]*"
            + control
            + rb")?"
            + rb"(?:\xe2\x9d\xaf|>)"
            + control
            + rb"[ \t]*"
        )
        replacement = name.encode("utf-8") + b" > "
    else:
        prompt_atom = (
            control
            + rb"(?:[a-z0-9][a-z0-9_-]{0,63}"
            + control
            + rb"[ \t]*)?"
            + control
            + rb"(?:\xe2\x9d\xaf|>|[a-z0-9][a-z0-9_-]{0,63}[ \t]*>)"
            + control
            + rb"[ \t]*"
        )
        replacement = b"> "
    return re.sub(rb"(?:" + prompt_atom + rb"){2,}", replacement, data, flags=re.IGNORECASE)


def _compact_prompt_only_replay_lines(data: bytes, name: str | None = None) -> bytes:
    """Collapse prompt redraw replay frames to a single live prompt."""
    if not data:
        return data

    parts = re.split(rb"(\r\n|\n|\r)", data)
    output: list[bytes] = []
    frame_run: list[tuple[str, bytes]] = []

    def flush_frame_run() -> None:
        nonlocal frame_run
        if not frame_run:
            return
        prompt_entries = [entry for kind, entry in frame_run if kind == "prompt"]
        if len(prompt_entries) > 1:
            output.append(prompt_entries[-1])
        else:
            output.extend(entry for _, entry in frame_run)
        frame_run = []

    for idx in range(0, len(parts), 2):
        chunk = _compact_inline_prompt_replay_runs(parts[idx], name)
        sep = parts[idx + 1] if idx + 1 < len(parts) else b""
        entry = chunk + sep
        if _is_prompt_only_replay_chunk(chunk, name):
            frame_run.append(("prompt", entry))
            continue
        if _is_prompt_frame_separator_chunk(chunk):
            frame_run.append(("separator", entry))
            continue
        if frame_run and _is_prompt_frame_separator_fragment_chunk(chunk):
            frame_run.append(("separator", entry))
            continue
        if frame_run and _is_prompt_frame_blank_chunk(chunk):
            frame_run.append(("separator", entry))
            continue
        flush_frame_run()
        output.append(entry)
    flush_frame_run()
    return b"".join(output)


def _strip_stale_prompt_prefixes_from_replay_lines(data: bytes, name: str | None = None) -> bytes:
    """Remove repeated prompt prefixes that were accidentally captured before output lines.

    A real command line is ``profile > command`` (single space after the prompt).
    The broken replay shape we see after resize/reattach is many lines like
    ``profile ❯     actual output``. Native terminals overwrote the prompt; the
    raw replay buffer preserved it before each rendered output row. Only strip
    this shape when it repeats, and require at least two spaces after the prompt
    so pending/real command input stays intact.
    """
    if not data:
        return data
    named = b""
    if name and _PROFILE_ID_RE.fullmatch(name):
        named = re.escape(name.encode("utf-8")) + _PTY_CONTROL_FRAGMENT_RE + rb"[ \t]*"
    prompt = (
        rb"(?P<prefix>(?:^|[\r\n]))"
        + _PTY_CONTROL_FRAGMENT_RE
        + rb"(?:"
        + named
        + rb")?"
        + rb"(?:\xe2\x9d\xaf|>)"
        + _PTY_CONTROL_FRAGMENT_RE
        + rb"[ \t]{2,}"
    )
    pattern = re.compile(prompt, flags=re.IGNORECASE)
    if len(pattern.findall(data)) < 3:
        return data
    return pattern.sub(lambda match: match.group("prefix"), data)


def _drop_stale_fullscreen_editor_replay(data: bytes, name: str | None = None) -> bytes:
    """全屏编辑器退出后，只在重连快照里丢掉旧的编辑器屏幕。

    Pico/nano 这类 TUI 会用全屏重绘控制序列接管终端。真实终端退出后会恢复屏幕，
    但我们的 PTY buffer 是最近 1MB 原始字节；重连时逐字回放旧字节，会把已经退出的
    编辑器画面“复活”在网页里。这里不碰活流：只有快照同时满足
    1) 含全屏编辑器特征；2) 特征之后已经出现该 profile 的 shell prompt
    才裁剪到最后一个 prompt，保留用户已经打在 prompt 后面的待发送文字。
    """
    if not data or not any(marker in data for marker in _FULLSCREEN_EDITOR_REPLAY_MARKERS):
        return data

    marker_pos = max(data.rfind(marker) for marker in _FULLSCREEN_EDITOR_REPLAY_MARKERS)
    if marker_pos < 0:
        return data

    prompt_re = _profile_prompt_re(name)
    last_prompt_start: int | None = None
    for match in prompt_re.finditer(data, marker_pos):
        last_prompt_start = match.start("prompt")
    if last_prompt_start is None and name:
        for match in _GENERIC_PROFILE_PROMPT_RE.finditer(data, marker_pos):
            last_prompt_start = match.start("prompt")
    if last_prompt_start is None:
        return data
    return data[last_prompt_start:]


def _sanitize_pty_replay_snapshot(data: bytes, name: str | None = None) -> bytes:
    data = _PTY_REPLAY_QUERY_RE.sub(b"", data)
    data = _neutralize_replay_screen_resets(data)
    data = _drop_hermes_status_replay_frames(data)
    data = _drop_hermes_progress_replay_frames(data)
    data = _compact_prompt_only_replay_lines(data, name)
    data = _strip_stale_prompt_prefixes_from_replay_lines(data, name)
    return _drop_stale_fullscreen_editor_replay(data, name)


class _PtyRuntime:
    """独立于 WebSocket 的长驻 PTY。

    浏览器切 agent / React 卸载只会解除订阅；Hermes 子进程继续在后台运行。
    同一个 terminal_id 再连接时会收到最近输出快照，并继续接管原 PTY。
    """

    def __init__(
        self,
        runtime_id: str,
        name: str,
        proc: subprocess.Popen,
        master_fd: int,
        rows: int,
        cols: int,
        *,
        carrier: str = "direct",
        screen_session: str | None = None,
        screen_bin: str | None = None,
    ):
        self.id = runtime_id
        self.name = name
        self.proc = proc
        self.master_fd = master_fd
        self.rows = rows
        self.cols = cols
        self.carrier = carrier
        self.screen_session = screen_session
        self.screen_bin = screen_bin
        self.buffer = bytearray(_load_pty_replay(runtime_id))
        self.subscribers: set[asyncio.Queue[bytes | None]] = set()
        self.closed = False
        self.write_lock = threading.Lock()
        self._strip_carry = b""  # 跨 os.read 边界被切断的设备响应前缀暂存
        self._size_votes: dict[int, tuple[int, int]] = {}  # 当前可见订阅者尺寸提案（取最小值仲裁）
        now = time.time()
        self.created_at = now
        self.last_attach_at = 0.0
        self.last_detach_at = 0.0
        self.last_output_at = 0.0
        os.set_blocking(master_fd, False)
        self.reader = asyncio.create_task(self._pump())

    def attach(self, *, replay: bool = True) -> tuple[asyncio.Queue[bytes | None], bytes]:
        # Terminal-faithful：浏览器端短暂卡顿或后台节流时不能丢 PTY 字节。
        # 旧版 maxsize=512 满后丢旧 chunk，会造成“模型其实回复了，
        # 页面不显示/刷新后才补一部分”的错觉。这里使用无界队列；
        # 进程级回放缓存仍由 _PTY_BUFFER_LIMIT 控制。
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.subscribers.add(queue)
        self.last_attach_at = time.time()
        if not replay:
            return queue, b""
        # 回放前剥离"终端能力查询"：查询只对发出当时的那个终端有意义。
        # 重连回放若带着查询，xterm 会再次应答，旧版前端曾把应答写回 PTY，
        # 被 TUI 当作文本输入（输入栏凭空出现 ]11;rgb:... / 空格的根因）。
        return queue, _sanitize_pty_replay_snapshot(_tail_pty_replay(bytes(self.buffer)), self.name)

    def detach(self, queue: asyncio.Queue[bytes | None]) -> None:
        self.subscribers.discard(queue)
        self.last_detach_at = time.time()
        if self._size_votes.pop(id(queue), None) is not None:
            self._apply_size_votes()

    def propose_size(self, queue: asyncio.Queue[bytes | None], rows: int, cols: int) -> None:
        """多视图共享同一 PTY 时的尺寸仲裁（tmux 语义：取可见视图的最小值）。

        没有仲裁的话，两个尺寸不同的可见视图会用各自的 resize 轮流覆盖对方，
        TUI 每次 SIGWINCH 都全量重绘 prompt——这就是"重复 prompt 幽灵行 +
        边框错位"的根因之一。隐藏标签/白板必须 release 投票，否则小窗口会把
        大窗口永久压窄。"""
        self._size_votes[id(queue)] = (rows, cols)
        self._apply_size_votes()

    def release_size(self, queue: asyncio.Queue[bytes | None]) -> None:
        if self._size_votes.pop(id(queue), None) is not None:
            self._apply_size_votes()

    def _apply_size_votes(self) -> None:
        if not self._size_votes:
            # 没有可见视图时保留最后一次真实终端尺寸。把后台 PTY 缩回
            # fallback(100x30) 会触发 Hermes TUI 重绘，导致切换/刷新后出现
            # prompt 上浮、右侧裁切、重复状态行等看似“前端卡住”的错位。
            return
        rows = min(r for r, _ in self._size_votes.values())
        cols = min(c for _, c in self._size_votes.values())
        self.resize(rows, cols)

    def write(self, data: str, *, suppress_echo: bool = False) -> bool:
        if self.closed:
            return False
        payload = data.encode()
        if not payload:
            return True
        with self.write_lock:
            original_attrs = None
            try:
                if suppress_echo:
                    original_attrs = termios.tcgetattr(self.master_fd)
                    quiet_attrs = copy.deepcopy(original_attrs)
                    quiet_attrs[3] &= ~termios.ECHO
                    termios.tcsetattr(self.master_fd, termios.TCSANOW, quiet_attrs)
                view = memoryview(payload)
                written = 0
                deadline = time.monotonic() + max(2.0, min(30.0, len(payload) / 4096))
                while written < len(payload):
                    try:
                        n = os.write(self.master_fd, view[written:])
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            return False
                        select.select([], [self.master_fd], [], 0.05)
                        continue
                    if n <= 0:
                        if time.monotonic() >= deadline:
                            return False
                        select.select([], [self.master_fd], [], 0.05)
                        continue
                    written += n
                # 注意：即使个别内核在恢复 ECHO 前没处理完输入导致应答被回显，
                # _pump 的流式剥离 + 前端剥离也会兜住，不会成为可见乱码。
                return True
            except (OSError, termios.error):
                return False
            finally:
                if original_attrs is not None:
                    try:
                        termios.tcsetattr(self.master_fd, termios.TCSANOW, original_attrs)
                    except (OSError, termios.error):
                        pass

    def resize(self, rows: int, cols: int) -> None:
        if not self.closed:
            if self.rows == rows and self.cols == cols:
                return
            self.rows = rows
            self.cols = cols
            _set_winsize(self.master_fd, rows, cols)

    def _broadcast(self, data: bytes | None) -> None:
        for queue in tuple(self.subscribers):
            queue.put_nowait(data)

    def _finish(self) -> None:
        if self.closed:
            return
        self.closed = True
        if _PTY_RUNTIMES.get(self.id) is self:
            _PTY_RUNTIMES.pop(self.id, None)
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self._broadcast(None)

    async def _pump(self) -> None:
        try:
            while not self.closed:
                try:
                    data = os.read(self.master_fd, 16384)
                except BlockingIOError:
                    if self.proc.poll() is not None:
                        break
                    await asyncio.sleep(0.025)
                    continue
                except OSError:
                    break
                if not data:
                    break
                data = self._strip_carry + data
                self._strip_carry = b""
                # 先暂存末尾可疑的半截响应，再对其余部分剥离——顺序不能反：
                # 剥离器对终结符是宽容的，会提前吃掉"还没收完"的响应体，
                # 下一块才到的终结符就会泄漏成可见字符。
                carry = _device_response_carry_len(data)
                if carry:
                    self._strip_carry = bytes(data[-carry:])
                    data = data[:-carry]
                data = _strip_pty_device_responses(data)
                if not data:
                    continue
                self.last_output_at = time.time()
                self.buffer.extend(data)
                self._trim_buffer()
                _persist_pty_replay_append(self.id, data)
                self._broadcast(data)
        finally:
            leftover = _strip_pty_device_responses(self._strip_carry)
            self._strip_carry = b""
            if leftover:
                self.buffer.extend(leftover)
                self._trim_buffer()
                _persist_pty_replay_append(self.id, leftover)
                self._broadcast(leftover)
            self.proc.poll()
            self._finish()

    def _trim_buffer(self) -> None:
        """截断回放缓存时不能切在转义序列 / UTF-8 多字节字符中间，
        否则重连快照会以半截序列开头——这就是"旧污染重放后出现乱码"的来源。"""
        if len(self.buffer) <= _PTY_BUFFER_LIMIT:
            return
        del self.buffer[:-_PTY_BUFFER_LIMIT]
        nl = self.buffer.find(b"\n", 0, 8192)
        if nl != -1:
            del self.buffer[: nl + 1]
            runtime_id = getattr(self, "id", "")
            if runtime_id:
                _persist_pty_replay_replace(runtime_id, bytes(self.buffer))
            return
        # 找不到行边界时至少跳过 UTF-8 续字节，保证不从半个字符开始
        while self.buffer and (self.buffer[0] & 0xC0) == 0x80:
            del self.buffer[:1]
        runtime_id = getattr(self, "id", "")
        if runtime_id:
            _persist_pty_replay_replace(runtime_id, bytes(self.buffer))

    async def _terminate_attach_process(self) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        for _ in range(20):
            if self.proc.poll() is not None:
                break
            await asyncio.sleep(0.025)
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            self.proc.poll()

    def _terminate_attach_process_sync(self) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        deadline = time.monotonic() + 0.75
        while self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.025)
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            self.proc.poll()

    def _close_runtime_shell(self) -> None:
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self._broadcast(None)
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if getattr(self, "reader", None) is not None and self.reader is not current_task:
            self.reader.cancel()

    async def detach_for_server_shutdown(self) -> None:
        """Backend shutdown should not mean "delete this chat".

        With the screen carrier, the real Hermes TTY stays alive and only the
        backend attach process is closed.  Direct runtimes cannot survive a
        backend restart, so they still fall back to the normal stop path.
        """
        if not self.screen_session:
            await self.stop()
            return
        if self.closed:
            return
        self.closed = True
        if _PTY_RUNTIMES.get(self.id) is self:
            _PTY_RUNTIMES.pop(self.id, None)
        await self._terminate_attach_process()
        self._close_runtime_shell()

    def detach_for_server_shutdown_sync(self) -> None:
        if not self.screen_session:
            self.stop_sync()
            return
        if self.closed and self.proc.poll() is not None:
            return
        self.closed = True
        if _PTY_RUNTIMES.get(self.id) is self:
            _PTY_RUNTIMES.pop(self.id, None)
        self._terminate_attach_process_sync()
        self._close_runtime_shell()

    async def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        if _PTY_RUNTIMES.get(self.id) is self:
            _PTY_RUNTIMES.pop(self.id, None)
        await self._terminate_attach_process()
        if self.screen_session:
            await asyncio.to_thread(_quit_screen_session, self.screen_session, self.screen_bin)
        self._close_runtime_shell()

    def stop_sync(self) -> None:
        """同步兜底回收 PTY；用于进程退出/atexit，不能依赖事件循环。"""
        if self.closed and self.proc.poll() is not None:
            return
        self.closed = True
        if _PTY_RUNTIMES.get(self.id) is self:
            _PTY_RUNTIMES.pop(self.id, None)
        self._terminate_attach_process_sync()
        if self.screen_session:
            _quit_screen_session(self.screen_session, self.screen_bin)
        self._close_runtime_shell()


def _start_pty_runtime(
    runtime_id: str,
    name: str,
    *,
    resume_sid: str | None,
    fresh: bool,
    rows: int = 30,
    cols: int = 100,
) -> _PtyRuntime:
    screen_bin = _pty_screen_bin()
    if screen_bin:
        try:
            return _start_screen_pty_runtime(
                runtime_id,
                name,
                resume_sid=resume_sid,
                fresh=fresh,
                rows=rows,
                cols=cols,
                screen_bin=screen_bin,
            )
        except Exception as exc:
            print(f"[pty] screen carrier failed for {runtime_id}: {exc}; falling back to direct", file=sys.stderr, flush=True)
    return _start_direct_pty_runtime(runtime_id, name, resume_sid=resume_sid, fresh=fresh, rows=rows, cols=cols)


def _start_direct_pty_runtime(
    runtime_id: str,
    name: str,
    *,
    resume_sid: str | None,
    fresh: bool,
    rows: int = 30,
    cols: int = 100,
) -> _PtyRuntime:
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, rows, cols)
    try:
        proc = subprocess.Popen(
            _chat_argv(name, resume_sid=resume_sid, fresh=fresh),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=_chat_env(),
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)
    runtime = _PtyRuntime(runtime_id, name, proc, master_fd, rows, cols)
    _PTY_RUNTIMES[runtime_id] = runtime
    return runtime


def _start_screen_pty_runtime(
    runtime_id: str,
    name: str,
    *,
    resume_sid: str | None,
    fresh: bool,
    rows: int = 30,
    cols: int = 100,
    screen_bin: str,
) -> _PtyRuntime:
    session_name = _screen_session_name(runtime_id)
    env = _screen_env(_chat_env())
    if not _screen_session_exists(session_name, screen_bin):
        completed = subprocess.run(
            [screen_bin, "-S", session_name, "-dm", *_chat_argv(name, resume_sid=resume_sid, fresh=fresh)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            env=env,
            cwd=str(HERE),
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or f"screen exited {completed.returncode}").strip()
            raise RuntimeError(detail[:240])
        time.sleep(0.05)
        if not _screen_session_exists(session_name, screen_bin):
            raise RuntimeError("screen session exited before attach")

    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, rows, cols)
    try:
        proc = subprocess.Popen(
            [screen_bin, "-x", session_name],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=env,
            cwd=str(HERE),
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)
    runtime = _PtyRuntime(
        runtime_id,
        name,
        proc,
        master_fd,
        rows,
        cols,
        carrier="screen",
        screen_session=session_name,
        screen_bin=screen_bin,
    )
    _PTY_RUNTIMES[runtime_id] = runtime
    return runtime


def _get_or_start_pty_runtime(
    runtime_id: str,
    name: str,
    *,
    resume_sid: str | None,
    fresh: bool,
    rows: int = 30,
    cols: int = 100,
) -> _PtyRuntime:
    runtime = _PTY_RUNTIMES.get(runtime_id)
    if runtime and not runtime.closed and runtime.proc.poll() is None:
        if runtime.name != name:
            raise ValueError("PTY 运行时与 agent 不匹配")
        return runtime
    if runtime:
        runtime._finish()
    return _start_pty_runtime(runtime_id, name, resume_sid=resume_sid, fresh=fresh, rows=rows, cols=cols)


def _clamp_pty_size(rows: int | str | None, cols: int | str | None) -> tuple[int, int]:
    try:
        r = int(rows) if rows is not None else _PTY_FALLBACK_ROWS
    except (TypeError, ValueError):
        r = _PTY_FALLBACK_ROWS
    try:
        c = int(cols) if cols is not None else _PTY_FALLBACK_COLS
    except (TypeError, ValueError):
        c = _PTY_FALLBACK_COLS
    # 上限放宽到大屏/小字号也够用——夹得太死会造成 PTY 尺寸 ≠ xterm 实际尺寸，
    # TUI 按错误尺寸绘制导致右侧截断/边框错位。
    return max(10, min(240, r)), max(48, min(500, c))


@app.on_event("shutdown")
async def _shutdown_pty_runtimes():
    await asyncio.gather(*(runtime.detach_for_server_shutdown() for runtime in tuple(_PTY_RUNTIMES.values())), return_exceptions=True)


def _shutdown_pty_runtimes_sync(reason: str = "process-exit") -> int:
    runtimes = tuple(_PTY_RUNTIMES.values())
    if not runtimes:
        return 0
    for runtime in runtimes:
        runtime.detach_for_server_shutdown_sync()
    print(f"[pty] cleaned {len(runtimes)} runtime(s) during {reason}", file=sys.stderr, flush=True)
    return len(runtimes)


@app.get("/api/health")
def health():
    return {"ok": True, "hermes_bin": HERMES_BIN, "hermes_home": str(HERMES_HOME)}


def _pty_preview_path(raw_path: str) -> Path:
    """Resolve a terminal-emitted local path without exposing arbitrary user files."""
    if not raw_path or "\x00" in raw_path:
        raise HTTPException(400, "非法文件路径")
    candidate = Path(os.path.expanduser(raw_path)).resolve()
    if candidate.suffix.lower() not in _PTY_PREVIEW_EXTS:
        raise HTTPException(415, "该文件类型不支持网页预览")
    if not candidate.is_file():
        raise HTTPException(404, "文件不存在")
    for root in _PTY_PREVIEW_ROOTS:
        try:
            candidate.relative_to(root.resolve())
            return candidate
        except ValueError:
            continue
    raise HTTPException(403, "只允许预览 Hermes outputs 与系统临时目录中的文件")


def _safe_upload_filename(raw_name: str | None) -> str:
    name = Path(raw_name or "attachment").name.strip().replace("\x00", "")
    if not name or name in {".", ".."}:
        name = "attachment"
    suffix = Path(name).suffix.lower()
    if suffix not in _PTY_UPLOAD_EXTS:
        raise HTTPException(415, "该附件类型暂不支持上传")
    stem = Path(name).stem
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", stem).strip(" .-_") or "attachment"
    stem = stem[:80]
    return f"{stem}{suffix}"


def _pty_upload_target(runtime_id: str, filename: str) -> Path:
    if not _PTY_RUNTIME_ID_RE.match(runtime_id):
        raise HTTPException(400, "非法 PTY 运行时 ID")
    safe_runtime = re.sub(r"[^A-Za-z0-9_.:-]+", "-", runtime_id)[:120]
    upload_dir = (PTY_UPLOAD_DIR / safe_runtime).resolve()
    root = PTY_UPLOAD_DIR.resolve()
    try:
        upload_dir.relative_to(root)
    except ValueError:
        raise HTTPException(400, "非法上传目录")
    upload_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = upload_dir / f"{stamp}_{filename}"
    counter = 1
    while target.exists():
        target = upload_dir / f"{stamp}_{counter}_{filename}"
        counter += 1
    return target


@app.post("/api/pty/upload")
async def api_pty_upload(runtime_id: str = Form(...), file: UploadFile = File(...)):
    """浏览器文件上传桥：保存到 Hermes outputs，再把路径交给前端送入真 PTY。

    PTY 本身只是字节流，不可能读取浏览器沙盒里的本地文件；因此网页端必须先
    把用户拖入终端的文件复制到后端安全目录，再把这个真实路径写进 CLI。
    """
    filename = _safe_upload_filename(file.filename)
    target = _pty_upload_target(runtime_id, filename)
    size = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _PTY_UPLOAD_LIMIT:
                    try:
                        target.unlink()
                    except OSError:
                        pass
                    raise HTTPException(413, f"附件超过 {_PTY_UPLOAD_LIMIT // 1024 // 1024}MB 限制")
                out.write(chunk)
    finally:
        await file.close()
    return {
        "ok": True,
        "path": str(target),
        "name": target.name,
        "size": size,
        "previewable": target.suffix.lower() in _PTY_PREVIEW_EXTS,
    }


def _pty_text_preview_response(target: Path) -> HTMLResponse:
    """Render text-like local artifacts in the dashboard theme instead of browser defaults."""
    raw = target.read_bytes()
    truncated = len(raw) > _PTY_TEXT_PREVIEW_LIMIT
    preview_bytes = raw[:_PTY_TEXT_PREVIEW_LIMIT]
    text = preview_bytes.decode("utf-8", errors="replace")

    if target.suffix.lower() == ".json" and not truncated:
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass

    escaped_name = html.escape(target.name)
    escaped_path = html.escape(str(target))
    escaped_text = html.escape(text)
    truncated_note = (
        f"<div class=\"notice\">文件超过 {_PTY_TEXT_PREVIEW_LIMIT // 1024 // 1024}MB，"
        "这里只显示前半部分；原始文件没有被修改。</div>"
        if truncated
        else ""
    )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {{
      color-scheme: dark;
      --bg: #16180f;
      --panel: #1d2118;
      --fg: #edeadd;
      --muted: rgba(237, 234, 221, .58);
      --line: rgba(237, 234, 221, .16);
      --accent: #cbee52;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ min-height: 100%; margin: 0; background: var(--bg); }}
    body {{
      padding: 18px;
      color: var(--fg);
      font: 500 13px/1.62 ui-monospace, "SF Mono", SFMono-Regular, Menlo, Monaco, Consolas,
        "PingFang SC", "Noto Sans Mono CJK SC", monospace;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 1;
      margin: -18px -18px 14px;
      padding: 14px 18px 12px;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--panel) 92%, transparent);
      backdrop-filter: blur(10px);
    }}
    .kicker {{
      color: var(--accent);
      font: 800 10px/1 ui-monospace, "SF Mono", monospace;
      letter-spacing: .18em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 7px 0 0;
      overflow-wrap: anywhere;
      color: var(--fg);
      font-size: 17px;
      line-height: 1.25;
    }}
    .path {{
      margin-top: 7px;
      overflow-wrap: anywhere;
      color: var(--muted);
      font-size: 11px;
    }}
    .notice {{
      margin: 0 0 12px;
      padding: 10px 12px;
      border-left: 3px solid var(--accent);
      background: rgba(203, 238, 82, .08);
      color: var(--fg);
    }}
    pre {{
      margin: 0;
      min-height: calc(100vh - 128px);
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255, 255, 255, .025);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-word;
      tab-size: 2;
    }}
  </style>
</head>
<body>
  <header>
    <div class="kicker">Local Artifact Preview</div>
    <h1>{escaped_name}</h1>
    <div class="path">{escaped_path}</div>
  </header>
  {truncated_note}
  <pre>{escaped_text}</pre>
</body>
</html>""",
        headers={
            "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


@app.get("/api/pty/file")
def api_pty_file(path: str):
    target = _pty_preview_path(path)
    if target.suffix.lower() in _PTY_TEXT_PREVIEW_EXTS:
        return _pty_text_preview_response(target)
    media_type = mimetypes.guess_type(target.name)[0] or "text/plain"
    return FileResponse(
        str(target),
        media_type=media_type,
        headers={"Content-Disposition": "inline"},
    )


@app.get("/api/pty/runtime/{runtime_id}")
def api_pty_runtime_status(runtime_id: str):
    """查询后台 PTY 是否真实存活；用于把 WS 传输断开和 Hermes 进程退出区分开。"""
    if not _PTY_RUNTIME_ID_RE.match(runtime_id):
        raise HTTPException(400, "非法 PTY 运行时 ID")
    runtime = _PTY_RUNTIMES.get(runtime_id)
    if not runtime:
        screen_session = _screen_session_name(runtime_id)
        if _screen_session_exists(screen_session):
            return {
                "ok": True,
                "exists": True,
                "active": True,
                "closed": False,
                "name": runtime_id.split(":", 1)[0],
                "pid": None,
                "returncode": None,
                "subscribers": 0,
                "buffer_bytes": len(_load_pty_replay(runtime_id)),
                "rows": None,
                "cols": None,
                "created_at": None,
                "last_attach_at": None,
                "last_detach_at": None,
                "last_output_at": None,
                "size_votes": [],
                "carrier": "screen-detached",
                "screen_session": screen_session,
            }
        return {"ok": True, "exists": False, "active": False}
    returncode = runtime.proc.poll()
    active = (not runtime.closed) and returncode is None
    return {
        "ok": True,
        "exists": True,
        "active": active,
        "closed": runtime.closed,
        "name": runtime.name,
        "pid": runtime.proc.pid,
        "returncode": returncode,
        "subscribers": len(runtime.subscribers),
        "buffer_bytes": len(runtime.buffer),
        "rows": runtime.rows,
        "cols": runtime.cols,
        "created_at": runtime.created_at,
        "last_attach_at": runtime.last_attach_at,
        "last_detach_at": runtime.last_detach_at,
        "last_output_at": runtime.last_output_at,
        "size_votes": list(runtime._size_votes.values()),
        "carrier": runtime.carrier,
        "screen_session": runtime.screen_session,
    }


@app.get("/api/pty/runtimes")
def api_pty_runtime_list():
    """只读诊断：列出当前后端真正持有的后台 PTY。

    这是为了区分“按设计保活的会话”和“前端误触发导致的泄漏”。不做任何回收，
    不改变用户明确要求的“只有关闭/结束/删除才退出后台”的生命周期。"""
    now = time.time()
    runtimes = []
    for runtime_id, runtime in sorted(_PTY_RUNTIMES.items()):
        returncode = runtime.proc.poll()
        active = (not runtime.closed) and returncode is None
        last_touch = max(runtime.created_at, runtime.last_attach_at, runtime.last_detach_at, runtime.last_output_at)
        runtimes.append({
            "id": runtime_id,
            "name": runtime.name,
            "pid": runtime.proc.pid,
            "active": active,
            "closed": runtime.closed,
            "returncode": returncode,
            "subscribers": len(runtime.subscribers),
            "buffer_bytes": len(runtime.buffer),
            "rows": runtime.rows,
            "cols": runtime.cols,
            "created_at": runtime.created_at,
            "last_attach_at": runtime.last_attach_at,
            "last_detach_at": runtime.last_detach_at,
            "last_output_at": runtime.last_output_at,
            "idle_seconds": max(0.0, now - last_touch),
            "size_votes": list(runtime._size_votes.values()),
            "carrier": runtime.carrier,
            "screen_session": runtime.screen_session,
        })
    for runtime_id in _known_pty_replay_runtime_ids():
        if runtime_id in _PTY_RUNTIMES:
            continue
        screen_session = _screen_session_name(runtime_id)
        if not _screen_session_exists(screen_session):
            continue
        try:
            replay_path = _pty_replay_path(runtime_id)
            idle_seconds = max(0.0, now - replay_path.stat().st_mtime)
        except (OSError, ValueError):
            idle_seconds = 0.0
        runtimes.append({
            "id": runtime_id,
            "name": runtime_id.split(":", 1)[0],
            "pid": None,
            "active": True,
            "closed": False,
            "returncode": None,
            "subscribers": 0,
            "buffer_bytes": len(_load_pty_replay(runtime_id)),
            "rows": None,
            "cols": None,
            "created_at": None,
            "last_attach_at": None,
            "last_detach_at": None,
            "last_output_at": None,
            "idle_seconds": idle_seconds,
            "size_votes": [],
            "carrier": "screen-detached",
            "screen_session": screen_session,
        })
    runtimes.sort(key=lambda item: (-int(item["subscribers"] or 0), str(item["id"])))
    return {
        "ok": True,
        "count": len(runtimes),
        "active_count": sum(1 for runtime in runtimes if runtime["active"]),
        "subscribed_count": sum(1 for runtime in runtimes if runtime["subscribers"] > 0),
        "runtimes": runtimes,
    }


@app.delete("/api/pty/runtime/{runtime_id}")
async def api_close_pty_runtime(runtime_id: str):
    """明确关闭一个后台 PTY。页面切换和 WebSocket 断开不会调用这里。"""
    if not _PTY_RUNTIME_ID_RE.match(runtime_id):
        raise HTTPException(400, "非法 PTY 运行时 ID")
    runtime = _PTY_RUNTIMES.get(runtime_id)
    closed = runtime is not None
    if runtime:
        await runtime.stop()
    else:
        closed = _quit_screen_session(_screen_session_name(runtime_id))
    _clear_pty_replay(runtime_id)
    return {"ok": True, "closed": closed}


@app.post("/api/pty/runtime/{runtime_id}/clear-buffer")
def api_clear_pty_runtime_buffer(runtime_id: str):
    """只清空网页重连时的回放缓存，不结束后台 Hermes 进程。"""
    if not _PTY_RUNTIME_ID_RE.match(runtime_id):
        raise HTTPException(400, "非法 PTY 运行时 ID")
    runtime = _PTY_RUNTIMES.get(runtime_id)
    if not runtime:
        _clear_pty_replay(runtime_id)
        return {"ok": True, "active": False, "cleared": 0}
    cleared = len(runtime.buffer)
    runtime.buffer.clear()
    _clear_pty_replay(runtime_id)
    return {
        "ok": True,
        "active": (not runtime.closed) and runtime.proc.poll() is None,
        "cleared": cleared,
        "rows": runtime.rows,
        "cols": runtime.cols,
    }


class PtyAdoptSessionReq(BaseModel):
    session_id: str


@app.post("/api/pty/runtime/{runtime_id}/adopt-session")
def api_adopt_pty_session(runtime_id: str, req: PtyAdoptSessionReq):
    """把新对话的临时 PTY ID 收敛到真实 session ID。

    Hermes 在 fresh chat 启动后才在终端输出里公布 session id；如果前端继续用
    new:<nonce> 作为 runtime id，刷新页面或切回已保存会话时就会出现“显示 A、
    输入却连着 B”的错位。认领只改 dashboard 的 runtime 索引，不碰 Hermes
    会话文件，也不重启后台进程。
    """
    if not _PTY_RUNTIME_ID_RE.match(runtime_id):
        raise HTTPException(400, "非法 PTY 运行时 ID")
    session_id = (req.session_id or "").strip()
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "非法会话 ID")
    runtime = _PTY_RUNTIMES.get(runtime_id)
    if not runtime or runtime.closed or runtime.proc.poll() is not None:
        return {"ok": True, "active": False, "runtime_id": runtime_id}
    target_id = f"{runtime.name}:{session_id}"
    if not _PTY_RUNTIME_ID_RE.match(target_id):
        raise HTTPException(400, "非法目标 PTY 运行时 ID")
    existing = _PTY_RUNTIMES.get(target_id)
    if existing is runtime:
        return {"ok": True, "active": True, "runtime_id": target_id, "session_id": session_id}
    if existing and not existing.closed and existing.proc.poll() is None:
        raise HTTPException(409, "目标 session 已有活跃 PTY，拒绝覆盖")
    if existing:
        existing._finish()
    if _PTY_RUNTIMES.get(runtime_id) is runtime:
        _PTY_RUNTIMES.pop(runtime_id, None)
    if runtime.screen_session:
        target_screen_session = _screen_session_name(target_id)
        if target_screen_session != runtime.screen_session:
            if _screen_session_exists(target_screen_session, runtime.screen_bin):
                _PTY_RUNTIMES[runtime_id] = runtime
                raise HTTPException(409, "目标 session 已有活跃 PTY carrier，拒绝覆盖")
            if not _rename_screen_session(runtime.screen_session, target_screen_session, runtime.screen_bin):
                _PTY_RUNTIMES[runtime_id] = runtime
                raise HTTPException(502, "PTY carrier 重命名失败，保留临时 runtime")
            runtime.screen_session = target_screen_session
    old_replay = _load_pty_replay(runtime_id)
    if old_replay and not _load_pty_replay(target_id):
        _persist_pty_replay_replace(target_id, old_replay)
    _clear_pty_replay(runtime_id)
    runtime.id = target_id
    _PTY_RUNTIMES[target_id] = runtime
    return {"ok": True, "active": True, "runtime_id": target_id, "session_id": session_id}


class PtyModelReq(BaseModel):
    model: str
    provider: str | None = None


@app.post("/api/pty/runtime/{runtime_id}/model")
async def api_switch_pty_model(runtime_id: str, req: PtyModelReq):
    """用 Hermes 原生 `/model` 热切换一个正在运行的 PTY，不重启会话。"""
    if not _PTY_RUNTIME_ID_RE.match(runtime_id):
        raise HTTPException(400, "非法 PTY 运行时 ID")
    model = (req.model or "").strip()
    provider = (req.provider or "").strip()
    if not _MODEL_TOKEN_RE.match(model):
        raise HTTPException(400, "非法模型名")
    if provider and not _MODEL_TOKEN_RE.match(provider):
        raise HTTPException(400, "非法 provider 名")
    known = _known_model(model)
    if not known:
        raise HTTPException(400, "模型不在候选白名单中")
    provider = known.get("provider") or provider
    runtime = _PTY_RUNTIMES.get(runtime_id)
    if not runtime or runtime.closed or runtime.proc.poll() is not None:
        return {"ok": True, "active": False, "model": model}
    command = f"/model {model}"
    if provider:
        command += f" --provider {provider}"
    if not runtime.write(command + "\r"):
        raise HTTPException(409, "当前 PTY 无法接收模型切换")
    return {"ok": True, "active": True, "model": model, "provider": provider or None}


class PtyReasoningReq(BaseModel):
    effort: str


@app.post("/api/pty/runtime/{runtime_id}/reasoning")
async def api_switch_pty_reasoning(runtime_id: str, req: PtyReasoningReq):
    """用 Hermes 原生 `/reasoning` 热切换当前 PTY 的推理强度。"""
    if not _PTY_RUNTIME_ID_RE.match(runtime_id):
        raise HTTPException(400, "非法 PTY 运行时 ID")
    effort = (req.effort or "").strip().lower()
    if effort not in _REASONING_EFFORTS:
        raise HTTPException(400, f"未知推理强度: {effort}")
    runtime = _PTY_RUNTIMES.get(runtime_id)
    if not runtime or runtime.closed or runtime.proc.poll() is not None:
        return {"ok": True, "active": False, "effort": effort}
    if not runtime.write(f"/reasoning {effort}\r"):
        raise HTTPException(409, "当前 PTY 无法接收推理强度切换")
    return {"ok": True, "active": True, "effort": effort}


@app.get("/api/profiles")
def api_profiles():
    rows, source, source_error = build_profiles()
    h = _load_hierarchy()
    ks = _load_killed()
    drafts = _load_drafts()
    labels = _load_labels()
    pinned = set(_load_pinned())
    for r in rows:
        r["label"] = _label(r["name"], labels)
        r["is_pinned"] = r["name"] in pinned
        r["in_network"] = r.get("role") != "none"
        r["parent"] = effective_parent(r["name"], h)
        r["symlinks"] = skill_symlinks(r["name"])
        r["killed"] = r["name"] in ks
        r["effective_killed"] = effective_killed(r["name"], ks, h)
        r["main_twin"] = is_main_twin(r["name"])
        r["is_draft"] = r["name"] in drafts
    return {
        "profiles": rows,
        "source": source,
        "source_error": source_error,
        "hermes_home": str(HERMES_HOME),
        "count": len(rows),
    }


@app.get("/api/network")
def api_network():
    """Agent 组织关系网（树）：parent/children/in_network/role/symlinks/skill_count。"""
    return build_tree()


class KillReq(BaseModel):
    name: str
    killed: bool = True


@app.post("/api/kill")
def api_kill(req: KillReq):
    """停用/恢复一个 agent（仪表盘级闸，可逆）。停用会级联冻结其子树。"""
    if req.name != "default" and not _PROFILE_ID_RE.match(req.name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(req.name).is_dir():
        raise HTTPException(404, "profile 不存在")
    ks = _load_killed()
    if req.killed:
        ks.add(req.name)
    else:
        ks.discard(req.name)
    _save_killed(ks)
    return {"ok": True, "name": req.name, "killed": req.name in ks}


class LinkReq(BaseModel):
    source: str
    skill: str
    target: str


class UnlinkReq(BaseModel):
    target: str
    skill: str


class MoveReq(BaseModel):
    node: str
    new_parent: str | None = None
    drop: list[str] = []                              # 要从 node 删除的 symlink 技能名（如老父 core）
    add: list[dict] = []                              # 要引入的技能：[{source, skill}]
    skip_inherit_keys: list[str] = []                 # 落位弹窗里用户取消勾选的 config 继承键 → 加进 block
    skill_inherit_off_change: bool | None = None      # 落位弹窗里 skill 自动继承总开关。True=关，False=开，None=不动


def _descendants(node: str, by: dict) -> set:
    seen, stack = set(), [node]
    while stack:
        for ch in by.get(stack.pop(), {}).get("children", []):
            if ch not in seen:
                seen.add(ch)
                stack.append(ch)
    return seen


@app.post("/api/link")
def api_link(req: LinkReq):
    """共享：在 target/skills/ 下加一条指向 source 那个技能的 symlink（让 target 也能用）。"""
    for nm in (req.source, req.target):
        if nm != "default" and not _PROFILE_ID_RE.match(nm):
            raise HTTPException(400, f"非法 profile 名: {nm}")
    if not _SKILL_ID_RE.match(req.skill):
        raise HTTPException(400, "非法技能名")
    if req.source == req.target:
        raise HTTPException(400, "source 与 target 相同")
    src = _profile_dir(req.source) / "skills" / req.skill
    if not src.exists():
        raise HTTPException(404, f"源技能不存在: {req.source}/{req.skill}")
    tdir = _profile_dir(req.target) / "skills"
    if not tdir.is_dir():
        raise HTTPException(404, f"目标无 skills 目录: {req.target}")
    dst = tdir / req.skill
    if dst.exists() or dst.is_symlink():
        raise HTTPException(409, f"目标已有同名技能: {req.target}/{req.skill}")
    try:
        os.symlink(os.path.realpath(str(src)), str(dst))
    except OSError as e:
        raise HTTPException(500, f"创建 symlink 失败: {e}")
    return {"ok": True, "linked": {"target": req.target, "skill": req.skill, "source": req.source}}


@app.delete("/api/link")
def api_unlink(req: UnlinkReq):
    """取消共享：删 target/skills/<skill>，但仅当它是 symlink（绝不删真实技能内容）。"""
    if req.target != "default" and not _PROFILE_ID_RE.match(req.target):
        raise HTTPException(400, "非法 profile 名")
    if not _SKILL_ID_RE.match(req.skill):
        raise HTTPException(400, "非法技能名")
    path = _profile_dir(req.target) / "skills" / req.skill
    if not path.is_symlink():
        raise HTTPException(409, "该技能不是 symlink —— 只解除共享，绝不删真实技能内容")
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(500, f"解除失败: {e}")
    return {"ok": True, "unlinked": {"target": req.target, "skill": req.skill}}


@app.post("/api/move")
def api_move(req: MoveReq):
    """把 node 挪到 new_parent 下（更新组织树元数据），并按选择丢/引技能 symlink。
    破坏性—前端必须二次确认。只动 symlink、不碰真实技能内容、可逆；防自链/环。"""
    node = req.node
    if node != "default" and not _PROFILE_ID_RE.match(node):
        raise HTTPException(400, "非法 node 名")
    if not _profile_dir(node).is_dir():
        raise HTTPException(404, "node 不存在")
    np = req.new_parent or None
    if np is not None:
        if np != "default" and not _PROFILE_ID_RE.match(np):
            raise HTTPException(400, "非法 new_parent 名")
        if not _profile_dir(np).is_dir():
            raise HTTPException(404, "new_parent 不存在")
        if np == node:
            raise HTTPException(400, "不能把自己设为自己的父级")
        if np in _descendants(node, build_tree()["nodes"]):
            raise HTTPException(409, "会产生循环引用（new_parent 在 node 的子树里）")
    # 预校验 drop（只允许删 symlink）与 add（源必须真有该技能）
    for sk in req.drop:
        if not _SKILL_ID_RE.match(sk):
            raise HTTPException(400, f"非法技能名: {sk}")
        p = _profile_dir(node) / "skills" / sk
        if p.exists() and not p.is_symlink():
            raise HTTPException(409, f"{node}/{sk} 不是 symlink，拒绝删除（不碰真实技能）")
    adds = []
    for a in req.add:
        src = str(a.get("source") or "").strip()
        skill = str(a.get("skill") or "").strip()
        if (src != "default" and not _PROFILE_ID_RE.match(src)) or not _SKILL_ID_RE.match(skill):
            raise HTTPException(400, f"非法 add 项: {a}")
        srcp = _profile_dir(src) / "skills" / skill
        if not srcp.exists():
            raise HTTPException(404, f"源技能不存在: {src}/{skill}")
        adds.append((src, skill, srcp))
    # 执行
    warnings, dropped, added = [], [], []
    for sk in req.drop:
        p = _profile_dir(node) / "skills" / sk
        if p.is_symlink():
            try:
                p.unlink()
                dropped.append(sk)
            except OSError as e:
                warnings.append(f"删除 {sk} 失败: {e}")
        else:
            warnings.append(f"{sk} 不存在或非 symlink，跳过")
    for src, skill, srcp in adds:
        dst = _profile_dir(node) / "skills" / skill
        if dst.exists() or dst.is_symlink():
            warnings.append(f"{node} 已有 {skill}，跳过引入")
            continue
        try:
            os.symlink(os.path.realpath(str(srcp)), str(dst))
            added.append({"source": src, "skill": skill})
        except OSError as e:
            warnings.append(f"引入 {skill} 失败: {e}")
    h = _load_hierarchy()
    h[node] = np            # 显式记录组织树父级（None=顶层），与技能 symlink 解耦
    _save_hierarchy(h)
    # 草稿 agent 落位后清除草稿标记（"placed"，不再游离）
    drafts = _load_drafts()
    was_draft = node in drafts
    if was_draft:
        drafts.discard(node)
        _save_drafts(drafts)
    # 落位弹窗里取消勾选的 config 继承键 → 冻结当前值为自有（child-wins）+ 加进 block，未来 daemon 不再回填
    blocked_added = []
    if req.skip_inherit_keys:
        sane = [k for k in req.skip_inherit_keys if k in _INHERITABLE_KEYS]
        if sane:
            # 抢在 hierarchy 变更后、materialize 前，把当前的有效值锁进 own：先读 cur config + clear 该键的 track
            cfg = _read_config(node)
            track = _load_inherit_track(node)
            for k in sane:
                track.pop(k, None)                     # 清 track → _own_config 把它视为 own
                # cfg[k] 当前值就是上一次物化的结果（若没物化过则可能不在 cfg）；保留即"冻结"
            _write_config(node, cfg)
            _save_inherit_track(node, track)
            # 写 block 列表
            ball = _load_config_block_all()
            cur_block = set(ball.get(node, []))
            cur_block.update(sane)
            ball[node] = sorted(cur_block)
            _save_config_block_all(ball)
            blocked_added = sane
    # 落位弹窗里的 skill 自动继承总开关
    skill_inherit_changed = None
    if req.skill_inherit_off_change is not None:
        off = _load_skill_inherit_off()
        prev = node in off
        if req.skill_inherit_off_change:
            off.add(node)
        else:
            off.discard(node)
        if (node in off) != prev:
            _save_skill_inherit_off(off)
            skill_inherit_changed = "off" if req.skill_inherit_off_change else "on"
    # 立即沿树物化（让 block / 新父辈值 / external_dirs 实时生效，不等 daemon 5s）
    _materialize_tree(dry_run=False)
    _materialize_constitution(node)   # reparent 改了祖先链 → 重刷 node 及其子树的有效宪法注入块
    return {"ok": True, "node": node, "new_parent": np,
            "dropped": dropped, "added": added, "warnings": warnings,
            "blocked_inherit": blocked_added,
            "skill_inherit_changed": skill_inherit_changed,
            "was_draft": was_draft}


class CreateEmptyReq(BaseModel):
    name: str
    description: str | None = None
    display: str | None = None     # 显示名（任意语言）；底层仍用小写 name，仅写 labels.json


@app.post("/api/agent/create-empty")
def api_create_empty(req: CreateEmptyReq):
    """新建"空 agent"（草稿态）：跑 hermes profile create --no-skills --no-alias，
       同时加进 drafts.json。草稿 effective_parent 显式为 None（不挂 default、不参与树继承），
       用户随后拖到位置时 /api/move 自动清掉草稿标记。
       display：可选显示名（中文等）→ 写 labels.json；底层 ID 仍是小写 name。"""
    name = (req.name or "").strip()
    if not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名（连字符小写、长度 1-32）")
    if name == "default":
        raise HTTPException(409, "default 是主 agent，不可新建同名")
    if _profile_dir(name).is_dir():
        raise HTTPException(409, f"profile {name} 已存在")
    desc = (req.description or "").strip()
    args = ["profile", "create", name, "--no-skills", "--no-alias"]
    if desc:
        args.extend(["--description", desc])
    rc, out, err = run_hermes(args, timeout=30)
    if rc != 0:
        raise HTTPException(500, f"hermes profile create 失败: {(err or out or '未知错误').strip()[:300]}")
    if not _profile_dir(name).is_dir():
        raise HTTPException(500, "hermes profile create 返回成功但 profile 目录未生成")
    _invalidate_profile_list_cache()
    if req.display:
        _set_label(name, req.display)            # 写显示名映射
    # 加进草稿名单（hierarchy.json 不写入；effective_parent 自动返回 None）
    drafts = _load_drafts()
    drafts.add(name)
    _save_drafts(drafts)
    return {"ok": True, "name": name, "label": _label(name), "is_draft": True,
            "description": desc, "hermes_stdout": out.strip()[:300]}


# --------------------------------------------------------------------------- #
# 侧栏 ⋯ 菜单：pin（置顶分身，原位不动）/ rename（改显示名）/ fork（克隆）/ delete（归档）
# --------------------------------------------------------------------------- #
PINNED_FILE = HERE / "pinned.json"


def _load_pinned() -> list:
    try:
        d = json.loads(PINNED_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save_pinned(lst: list) -> None:
    try:
        PINNED_FILE.write_text(json.dumps(lst, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


class PinReq(BaseModel):
    pinned: bool


@app.post("/api/agent/{name}/pin")
def api_pin(name: str, req: PinReq):
    """置顶/取消置顶。置顶 = 在侧栏顶部放一个快捷"分身"，点它跳到真 profile；
       原 profile 在树里的位置不动（pin 不是移动，是加个快捷方式）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    pinned = _load_pinned()
    if req.pinned and name not in pinned:
        pinned.append(name)
    elif not req.pinned and name in pinned:
        pinned.remove(name)
    _save_pinned(pinned)
    return {"ok": True, "name": name, "pinned": name in pinned, "pinned_list": pinned}


class RenameReq(BaseModel):
    display: str   # 只改显示名（labels.json）；底层 ID 不变（改 ID 是重操作，走 CLI/单独流程）


@app.post("/api/agent/{name}/rename")
def api_rename(name: str, req: RenameReq):
    """改显示名（仅 labels.json，即时安全）。底层 profile ID 不变——改 ID 会牵动 symlink/继承路径，不在快捷菜单里做。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir() and name != "default":
        raise HTTPException(404, "profile 不存在")
    _set_label(name, req.display)
    return {"ok": True, "name": name, "label": _label(name)}


class ForkReq(BaseModel):
    new_name: str               # 新 profile 的小写 ID
    new_display: str | None = None


@app.post("/api/agent/{name}/fork")
def api_fork(name: str, req: ForkReq):
    """克隆一个 agent：hermes profile create <new> --clone-from <source>。
       新 agent 落在和源同一个父级下（同级），继承源的全部 config；显示名可另起。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法源 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "源 profile 不存在")
    new = (req.new_name or "").strip()
    if not _PROFILE_ID_RE.match(new):
        raise HTTPException(400, "非法新 profile 名（小写连字符）")
    if new == "default" or _profile_dir(new).is_dir():
        raise HTTPException(409, f"目标名已存在: {new}")
    rc, out, err = run_hermes(["profile", "create", new, "--clone-from", name, "--no-alias"], timeout=40)
    if rc != 0 or not _profile_dir(new).is_dir():
        raise HTTPException(500, f"克隆失败: {(err or out or '未知错误').strip()[:300]}")
    _invalidate_profile_list_cache()
    # 落在与源同一父级下（同级）
    h = _load_hierarchy()
    h[new] = effective_parent(name, h)
    _save_hierarchy(h)
    if req.new_display:
        _set_label(new, req.new_display)
    _materialize_tree(dry_run=False)
    _convert_to_inherit(new, dry_run=False)   # 默认继承：剥掉 clone 来的 bundled 副本，改吃 external_dirs 继承
    return {"ok": True, "new": new, "label": _label(new), "parent": h[new],
            "cloned_from": name}


@app.delete("/api/agent/{name}")
def api_delete_agent(name: str):
    """删除一个 agent：hermes profile delete（归档到 trash，可恢复）+ 清掉所有 dashboard 状态引用。
       有子节点时拒绝（先让用户处理子节点，避免悬空）。default 不可删。"""
    if name == "default":
        raise HTTPException(400, "default 是主 agent，不可删除")
    if not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    # 有子节点 → 拒绝（防悬空）
    h = _load_hierarchy()
    kids = [k for k, v in h.items() if v == name]
    if kids:
        raise HTTPException(409, f"该 agent 还有下属 {kids}，请先移走或删除它们")
    rc, out, err = run_hermes(["profile", "delete", name, "-y"], timeout=30)
    if rc != 0 and _profile_dir(name).is_dir():
        raise HTTPException(500, f"删除失败: {(err or out or '未知错误').strip()[:300]}")
    _invalidate_profile_list_cache()
    # 清 dashboard 状态：hierarchy / labels / pinned / drafts / killed / config_block / skill_off / 宪法订阅
    h.pop(name, None)
    _save_hierarchy(h)
    labels = _load_labels(); labels.pop(name, None); _save_labels(labels)
    pinned = _load_pinned()
    if name in pinned:
        pinned.remove(name); _save_pinned(pinned)
    drafts = _load_drafts()
    if name in drafts:
        drafts.discard(name); _save_drafts(drafts)
    ks = _load_killed()
    if name in ks:
        ks.discard(name); _save_killed(ks)
    block = _load_config_block_all()
    if name in block:
        block.pop(name); _save_config_block_all(block)
    off = _load_skill_inherit_off()
    if name in off:
        off.discard(name); _save_skill_inherit_off(off)
    return {"ok": True, "deleted": name}


# --------------------------------------------------------------------------- #
# 运行设置 4 杠杆（per-agent）：model / memory 开关 / terminal 隔离档 / approvals 审批档
#   写进 agent 自有 config.yaml（child-wins）；选"继承"= 删掉自有键回退父辈/默认。
#   model 是软继承（只读父链默认，不物化进子 config）；memory 是真实继承键；
#   terminal + approvals 非继承键（本就本地）。
#   分身（config symlink→default）拒编辑——改它就是改 default。
# --------------------------------------------------------------------------- #
def _model_entry(value) -> dict | None:
    if isinstance(value, dict) and value.get("default"):
        entry = {
            "default": str(value.get("default")),
            "provider": value.get("provider") or None,
            "base_url": value.get("base_url") or None,
        }
        # ── 增强版模型目录字段（可选，向后兼容旧格式）──────────────
        rl = value.get("reasoning_levels")
        if isinstance(rl, list):
            entry["reasoning_levels"] = [str(l) for l in rl if str(l)]
        fm = value.get("fast_mode")
        if fm is not None:
            entry["fast_mode"] = bool(fm)
        cw = value.get("context_window")
        if isinstance(cw, int) and cw > 0:
            entry["context_window"] = cw
        for k in ("family", "tier", "description"):
            v = value.get(k)
            if v:
                entry[k] = str(v)
        return entry
    if isinstance(value, str) and value:
        return {"default": value, "provider": None, "base_url": None}
    return None


def _collect_known_models() -> list:
    """读取用户明确维护的模型候选白名单，并按模型名去重。"""
    out, seen = [], set()
    try:
        raw = json.loads(MODEL_OPTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = []
    for value in raw if isinstance(raw, list) else []:
        entry = _model_entry(value)
        if entry and entry["default"] not in seen:
            seen.add(entry["default"])
            out.append(entry)
    return out


def _model_for_id(model_id: str) -> dict | None:
    """在已知模型目录中按 model_id 查找模型条目。"""
    for entry in _collect_known_models():
        if entry.get("default") == model_id:
            return entry
    return None


def _model_reasoning_levels(model_id: str) -> list[str]:
    """返回指定模型支持的推理强度列表，若未在目录中则返回全部。"""
    entry = _model_for_id(model_id)
    if entry and isinstance(entry.get("reasoning_levels"), list):
        return entry["reasoning_levels"]
    # 回退：如果模型不在增强目录中，返回全部已知级别
    return sorted(_REASONING_EFFORTS)


def _known_model(default: str | None) -> dict | None:
    if not default:
        return None
    for entry in _collect_known_models():
        if entry.get("default") == default:
            return copy.deepcopy(entry)
    return None


def _canonical_model_entry(value) -> dict | None:
    entry = _model_entry(value)
    if not entry:
        return None
    known = _known_model(entry.get("default"))
    if not known:
        return entry
    if entry.get("provider"):
        known["provider"] = entry["provider"]
    if entry.get("base_url"):
        known["base_url"] = entry["base_url"]
    return known


def _effective_model_entry(name: str, cache: dict | None = None) -> dict | None:
    return _canonical_model_entry(_soft_effective_model(name, cache))


@app.get("/api/agent/{name}/levers")
def api_get_levers(name: str):
    """读 agent 的 4 个运行杠杆当前值 + 是否自有覆盖 + 可选模型清单。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    own = _own_config(name)
    eff = _effective_config(name)
    effective_model = _effective_model_entry(name)
    raw = _read_config(name)            # terminal/approvals 非继承，看自有文件
    mem = eff.get("memory") or {}
    term = raw.get("terminal") or {}
    appr = raw.get("approvals") or {}
    ag = eff.get("agent") or {}
    own_ag = own.get("agent") or {}
    model_id = effective_model.get("default") if isinstance(effective_model, dict) else None
    model_reasoning_levels = _model_reasoning_levels(model_id) if model_id else sorted(_REASONING_EFFORTS)
    model_meta = _model_for_id(model_id) or {}
    return {
        "name": name,
        "is_twin": is_main_twin(name),    # 分身不可单独编辑（config symlink）
        "model": effective_model,
        "model_own": "model" in own,
        "model_reasoning_levels": model_reasoning_levels,
        "model_fast_mode": bool(model_meta.get("fast_mode", False)),
        "fast_mode": str(ag.get("service_tier") or "").strip().lower() in {"fast", "priority"},
        "memory_enabled": bool(mem.get("memory_enabled", True)),
        "memory_own": "memory" in own,
        "terminal_backend": term.get("backend") or "local",
        "terminal_own": "terminal" in raw,
        "approvals_mode": appr.get("mode") or "manual",
        "approvals_own": "approvals" in raw,
        # 推理强度：agent.reasoning_effort（继承键 agent 的子项）；默认 medium
        "reasoning_effort": ag.get("reasoning_effort") or "medium",
        "reasoning_own": "reasoning_effort" in own_ag,
        "known_models": _collect_known_models(),
    }


class LeversReq(BaseModel):
    # 每个字段：值=设；字符串 "__inherit__"=删自有覆盖回退；None=不动
    model: dict | str | None = None
    memory_enabled: bool | str | None = None
    terminal_backend: str | None = None
    approvals_mode: str | None = None
    reasoning_effort: str | None = None
    fast_mode: bool | None = None


_TERMINAL_BACKENDS = {"local", "docker", "ssh", "modal", "daytona", "singularity"}
_APPROVALS_MODES = {"manual", "smart", "off"}
_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_INHERIT = "__inherit__"


@app.post("/api/agent/{name}/levers")
def api_set_levers(name: str, req: LeversReq):
    """写运行杠杆。model 是软继承：自有则写本地，继承则删本地并沿父链解析；
       memory 是真实继承键；terminal/approvals 是非继承本地键。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    if is_main_twin(name):
        raise HTTPException(409, "分身的 config 是 symlink 到 default，请直接编辑 default（X）")
    cfg = _read_config(name)
    track = _load_inherit_track(name)

    # model（软继承键，不参与 _INHERITABLE_KEYS 真实物化）
    if req.model == _INHERIT:
        cfg.pop("model", None); track.pop("model", None)
    elif isinstance(req.model, (dict, str)) and req.model:
        entry = _model_entry(req.model)
        if not entry:
            raise HTTPException(400, "非法模型配置")
        known = _known_model(entry.get("default"))
        if not known:
            raise HTTPException(400, "模型不在候选白名单中")
        cfg["model"] = known; track.pop("model", None)
        if not bool(known.get("fast_mode", False)):
            ag = dict(cfg.get("agent") or {})
            if str(ag.get("service_tier") or "").strip().lower() in {"fast", "priority"}:
                ag["service_tier"] = "standard"
                cfg["agent"] = ag
                track.pop("agent", None)

    # memory_enabled（继承键，嵌套）。只写最小增量到自有（hermes 运行时会和 DEFAULT_CONFIG 深合并，
    # 不会丢 limits 等其它 memory 子项）；这样 set→inherit 干净净零、config 不被冻结成大字典。
    if req.memory_enabled == _INHERIT:
        cfg.pop("memory", None); track.pop("memory", None)
    elif isinstance(req.memory_enabled, bool):
        mem = dict(cfg.get("memory") or {})       # 自有文件已有的 memory 子项（一般空）
        mem["memory_enabled"] = req.memory_enabled
        cfg["memory"] = mem; track.pop("memory", None)

    # terminal.backend（非继承键，纯本地）
    if req.terminal_backend == _INHERIT or req.terminal_backend == "local":
        cfg.pop("terminal", None)          # local 是 hermes 默认 → 不写 = 默认
    elif req.terminal_backend:
        if req.terminal_backend not in _TERMINAL_BACKENDS:
            raise HTTPException(400, f"未知终端后端: {req.terminal_backend}")
        t = dict(cfg.get("terminal") or {}); t["backend"] = req.terminal_backend; cfg["terminal"] = t

    # approvals.mode（非继承键，纯本地）
    if req.approvals_mode == _INHERIT or req.approvals_mode == "manual":
        cfg.pop("approvals", None)         # manual 是 hermes 默认
    elif req.approvals_mode:
        if req.approvals_mode not in _APPROVALS_MODES:
            raise HTTPException(400, f"未知审批档: {req.approvals_mode}")
        a = dict(cfg.get("approvals") or {}); a["mode"] = req.approvals_mode; cfg["approvals"] = a

    # agent.reasoning_effort（继承键 agent 是整键继承）。
    #   set：在物化下来的 agent 字典上加 reasoning_effort，清 track["agent"] → 整个 agent 转自有
    #        （代价：该 agent 从此不跟父级 agent.* 变化，这是整键覆盖的固有取舍，可接受）。
    #   inherit：整键 pop agent + track，下面 materialize 会从父级重新补回 → 干净回到"继承"。
    if req.reasoning_effort == _INHERIT:
        cfg.pop("agent", None); track.pop("agent", None)
    elif req.reasoning_effort:
        if req.reasoning_effort not in _REASONING_EFFORTS:
            raise HTTPException(400, f"未知推理强度: {req.reasoning_effort}")
        # 模型感知二次校验：检查当前 agent 所用模型是否支持该推理级别
        eff_model = _effective_model_entry(name)
        mid = eff_model.get("default") if isinstance(eff_model, dict) else None
        supported = _model_reasoning_levels(mid) if mid else sorted(_REASONING_EFFORTS)
        if req.reasoning_effort not in supported:
            raise HTTPException(
                400,
                f"模型 {mid or '未知'} 不支持推理强度 {req.reasoning_effort}。"
                f"支持: {', '.join(supported)}",
            )
        ag = dict(cfg.get("agent") or {})         # 物化下来的 agent（= 父级有效值）
        ag["reasoning_effort"] = req.reasoning_effort
        cfg["agent"] = ag
        track.pop("agent", None)

    # agent.service_tier（和 reasoning_effort 一样属于整键继承的 agent）。
    # Hermes 接受 human-facing "fast"，运行时映射为 Codex/OpenAI 的
    # service_tier="priority"；standard 是显式本地关闭。
    if isinstance(req.fast_mode, bool):
        eff_model = _canonical_model_entry(cfg.get("model")) or _effective_model_entry(name)
        mid = eff_model.get("default") if isinstance(eff_model, dict) else None
        meta = _model_for_id(mid) or {}
        if req.fast_mode and not bool(meta.get("fast_mode", False)):
            raise HTTPException(400, f"模型 {mid or '未知'} 不支持极速模式")
        ag = dict(cfg.get("agent") or {})
        ag["service_tier"] = "fast" if req.fast_mode else "standard"
        cfg["agent"] = ag
        track.pop("agent", None)

    # 备份 + 写盘
    cfgp = _profile_dir(name) / "config.yaml"
    bak = _profile_dir(name) / "config.yaml.dashbak"
    if not bak.is_file() and cfgp.is_file():
        try: bak.write_text(cfgp.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError: pass
    _write_config(name, cfg)
    _save_inherit_track(name, track)
    _materialize_config(name, {}, dry_run=False)   # 立即重物化：让 inherit 的键从父级补回、保持一致
    effective = _effective_config(name)
    effective_model = _effective_model_entry(name)
    effective_reasoning_effort = (effective.get("agent") or {}).get("reasoning_effort") or "medium"
    effective_fast_mode = str((effective.get("agent") or {}).get("service_tier") or "").strip().lower() in {"fast", "priority"}
    return {
        "ok": True,
        "name": name,
        "effective_model": effective_model,
        "effective_reasoning_effort": effective_reasoning_effort,
        "effective_fast_mode": effective_fast_mode,
    }


# --------------------------------------------------------------------------- #
# 记忆面板：per-agent 的 MEMORY.md（agent 自动沉淀的私脑）+ USER.md（对你的画像）。
#   记忆不继承、域隔离——这是"每 agent 独立持久记忆"的本体（核心能力）。
#   分身的 memories/ 是 symlink 到 default 的，编辑它=编辑共享记忆（标注提示）。
# --------------------------------------------------------------------------- #
def _memories_dir(name: str) -> Path:
    return _profile_dir(name) / "memories"


def _read_mem_file(name: str, fname: str) -> tuple:
    p = _memories_dir(name) / fname
    if not p.is_file():
        return "", 0, 0.0
    try:
        return p.read_text(encoding="utf-8", errors="ignore"), p.stat().st_size, p.stat().st_mtime
    except OSError:
        return "", 0, 0.0


@app.get("/api/agent/{name}/memory")
def api_get_memory(name: str):
    """读 agent 的 MEMORY.md + USER.md 内容 + 大小。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    mem, mem_sz, mem_mt = _read_mem_file(name, "MEMORY.md")
    usr, usr_sz, usr_mt = _read_mem_file(name, "USER.md")
    twin = is_main_twin(name)
    return {
        "name": name,
        "is_twin": twin,                          # 分身：记忆 symlink 到主 AI，是共享的
        "memory": mem, "memory_size": mem_sz, "memory_mtime": mem_mt,
        "user": usr, "user_size": usr_sz, "user_mtime": usr_mt,
    }


class MemoryReq(BaseModel):
    memory: str | None = None     # None=不动该文件
    user: str | None = None


@app.post("/api/agent/{name}/memory")
def api_set_memory(name: str, req: MemoryReq):
    """人在环：编辑/修正 agent 的记忆。写前 .membak 备份。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    mdir = _memories_dir(name)
    mdir.mkdir(parents=True, exist_ok=True)
    wrote = []
    for fname, content in (("MEMORY.md", req.memory), ("USER.md", req.user)):
        if content is None:
            continue
        p = mdir / fname
        if p.is_file():                            # 写前备份一次
            try:
                bak = p.with_suffix(p.suffix + ".membak")
                bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        try:
            p.write_text(content, encoding="utf-8")
            wrote.append(fname)
        except OSError as e:
            raise HTTPException(500, f"写 {fname} 失败: {e}")
    return {"ok": True, "name": name, "wrote": wrote}


# --------------------------------------------------------------------------- #
# 人格（SOUL.md 正文）就地编辑：SOUL = [受管宪法块] + [人格正文]。编辑器只动"人格正文"，
#   保存后自动重注入有效宪法块（防手滑删受管块）。分身重定向到 X（共享 SOUL）。
# --------------------------------------------------------------------------- #
@app.get("/api/agent/{name}/soul")
def api_get_soul(name: str):
    """读 agent 人格正文（SOUL.md 去掉受管宪法块）——供详情里就地编辑。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    sp = _profile_dir(name) / "SOUL.md"
    raw = sp.read_text(encoding="utf-8", errors="ignore") if sp.is_file() else ""
    return {"name": name, "persona": _strip_constitution(raw), "is_twin": is_main_twin(name), "has_soul": sp.is_file()}


class SoulReq(BaseModel):
    persona: str


@app.post("/api/agent/{name}/soul")
def api_set_soul(name: str, req: SoulReq):
    """就地保存人格正文：写 SOUL=人格，再重注入有效宪法块（受管块不被覆盖）。写前 .soulbak 备份。
       分身重定向到 X（与 X 共享 SOUL）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    target = MAIN_AGENT if is_main_twin(name) else name   # 分身=改 X
    sp = _profile_dir(target) / "SOUL.md"
    if sp.is_file():
        try:
            (_profile_dir(target) / "SOUL.md.soulbak").write_text(sp.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    try:
        sp.write_text((req.persona or "").strip() + "\n", encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"写入失败：{e}")
    _inject_soul(target)   # 人格正文上方重注入该 agent 的有效宪法块（若有）
    return {"ok": True, "name": target, "redirected": target != name}


@app.get("/api/memory/search")
def api_memory_search(q: str):
    """跨所有 agent 搜记忆——回答"哪个 agent 记得 X"。简单不区分大小写子串匹配。"""
    q = (q or "").strip()
    if not q:
        return {"q": "", "hits": []}
    ql = q.lower()
    hits = []
    for nm in _all_profile_names():
        for fname in ("MEMORY.md", "USER.md"):
            text, _, _ = _read_mem_file(nm, fname)
            if not text:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if ql in line.lower():
                    hits.append({"agent": nm, "file": fname, "line": i, "text": line.strip()[:200]})
                    if len(hits) >= 100:
                        break
    return {"q": q, "hits": hits, "count": len(hits)}


# --------------------------------------------------------------------------- #
# config.yaml 级继承（快照合并；目录类继承走 symlink 已是实时）
# 注意：_INHERITABLE_KEYS 真正定义在下方"沿组织树继承"小节（含完整 16 键）。
# --------------------------------------------------------------------------- #
_CONFIG_CACHE_LOCK = threading.RLock()
_CONFIG_CACHE: dict[str, tuple[int, int, dict]] = {}


def _invalidate_config_cache(name: str | None = None) -> None:
    with _CONFIG_CACHE_LOCK:
        if name is None:
            _CONFIG_CACHE.clear()
        else:
            _CONFIG_CACHE.pop(name, None)


def _read_config(name: str) -> dict:
    cfg = _profile_dir(name) / "config.yaml"
    try:
        st = cfg.stat()
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    with _CONFIG_CACHE_LOCK:
        cached = _CONFIG_CACHE.get(name)
        if cached and (cached[0], cached[1]) == key:
            return copy.deepcopy(cached[2])
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, yaml.YAMLError):
        return {}
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[name] = (key[0], key[1], copy.deepcopy(data))
    return data


def _write_config(name: str, data: dict) -> None:
    # config.yaml 是全系统最热的写（每次 tick / move / lever / warehouse 都写）。
    # 原子写：先写 .tmp 再 os.replace —— 并发读者（正在启动的 hermes chat、profile list）
    # 永远看到完整的旧或新文件，绝不会读到半截 → YAML 解析失败。
    # 持 _SYNC_LOCK(RLock) 与 5s tick 串行化，避免两个写者交错丢更新；RLock 允许 tick 嵌套重入。
    cfg = _profile_dir(name) / "config.yaml"
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    with _SYNC_LOCK:
        tmp = cfg.parent / (cfg.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, cfg)
    _invalidate_config_cache(name)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _merge_inherited_value(child_val, parent_val):
    """按类型合并"被继承的键值"（父级提供，子级保留自有）：
       dict → 深合并（父级覆盖同名叶子，子级独有键保留，如 mcp_servers 按 server 名并集）；
       list → 并集（保子级顺序，追加父级里没有的项，如 toolsets / fallback_providers）；
       其它标量 → 父级覆盖。
       关键：幂等——再合一次结果不变，所以后台轮询同步不会反复改写磁盘。
       hermes 原生 _deep_merge 对 list 是"整段覆盖"，会抹掉子级自有项；这里对 list 做并集，
       才贴合"全局基线 + 各 agent 追加"的子母集语义。"""
    if isinstance(child_val, dict) and isinstance(parent_val, dict):
        return _deep_merge(child_val, parent_val)
    if isinstance(child_val, list) and isinstance(parent_val, list):
        out = list(child_val)
        for x in parent_val:
            if x not in out:
                out.append(x)
        return out
    return copy.deepcopy(parent_val)


# --------------------------------------------------------------------------- #
# 沿组织树的 config 继承：组织树是唯一继承图，config 沿它自动流下来（子可覆盖）。
#   config 是单文件里的键、没法像 skill 目录逐个 symlink，所以靠"算 + 物化"：把有效 config
#   写进子的 config.yaml（hermes 只认 config.yaml）。用 .dash_inherited.json 记录"哪些键是
#   继承来的、写进去时是什么值"，从而：① 幂等（无变化不写）；② 父级变了能实时刷新；
#   ③ 子级改了某继承键(值≠记录) → 自动转"自有"，从此不再被父级覆盖（这就是"子可覆盖"）。
#   根(default)/分身(config symlink)跳过：default 是源，分身整份 symlink 已共享。
# --------------------------------------------------------------------------- #
_INHERITABLE_KEYS = [
    "providers", "fallback_providers", "credential_pool_strategies",
    "mcp_servers", "toolsets", "agent", "tool_loop_guardrails", "compression",
    "context", "prompt_caching", "auxiliary", "image_gen", "memory", "delegation", "curator", "hooks",
]
_INHERIT_TRACK = ".dash_inherited.json"


def _load_inherit_track(name: str) -> dict:
    try:
        d = json.loads((_profile_dir(name) / _INHERIT_TRACK).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_inherit_track(name: str, d: dict) -> None:
    try:
        (_profile_dir(name) / _INHERIT_TRACK).write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _own_config(name: str) -> dict:
    """子的"自有 config" = 当前 config.yaml 去掉"仍纯继承（值==上次写入记录）的键"。
       某继承键被改过(值≠记录) → 视为自有（= 子主动覆盖了它）。"""
    cur = _read_config(name)
    track = _load_inherit_track(name)
    return {k: v for k, v in cur.items() if not (k in track and track[k] == v)}


def _model_default_provider(value) -> tuple[str | None, str | None]:
    """从 config 的 model 值解析出 (default, provider)。支持 Hermes 的字符串/对象两种写法。"""
    if isinstance(value, dict):
        default = value.get("default")
        provider = value.get("provider")
        return (str(default) if default else None, str(provider) if provider else None)
    if isinstance(value, str) and value.strip():
        return value.strip(), None
    return None, None


def _soft_effective_model(name: str, cache: dict | None = None):
    """model 的软继承：自身 config.yaml 有自有 model 就用自身，否则沿组织树找父级。
    关键区别：不写入子 config.yaml，也不进入 .dash_inherited.json；手动切换后才成为本地覆盖。"""
    cache = {} if cache is None else cache
    if name in cache:
        return cache[name]
    cache[name] = None
    if is_main_twin(name):
        value = _soft_effective_model(MAIN_AGENT, cache)
    else:
        own = _own_config(name)
        if "model" in own:
            value = copy.deepcopy(own.get("model"))
        elif name == MAIN_AGENT:
            value = None
        else:
            value = _soft_effective_model(effective_parent(name) or MAIN_AGENT, cache)
    cache[name] = value
    return value


def read_effective_config_model(name: str, cache: dict | None = None):
    """返回软继承后的有效 model.default / provider；展示、对话杠杆、Profile API 用这个。"""
    return _model_default_provider(_effective_model_entry(name, cache))


SKILL_INHERIT_OFF_FILE = HERE / "skill_inherit_off.json"


def _load_skill_inherit_off() -> set:
    """关闭了 skill 自动继承的 agent 名集合（手动 opt-out）。"""
    try:
        d = json.loads(SKILL_INHERIT_OFF_FILE.read_text(encoding="utf-8"))
        return set(d) if isinstance(d, list) else set()
    except (OSError, ValueError):
        return set()


def _save_skill_inherit_off(names: set) -> None:
    try:
        SKILL_INHERIT_OFF_FILE.write_text(json.dumps(sorted(names), ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# 单键 opt-out：落位弹窗里用户取消勾选的 config 继承键。结构 {profile: [keys]}。
# _materialize_config 跳过这些键 → 即便父辈有，子也不被覆盖；child-wins 兜底保留旧值。
CONFIG_BLOCK_FILE = HERE / "config_inherit_block.json"


def _load_config_block_all() -> dict:
    try:
        d = json.loads(CONFIG_BLOCK_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config_block_all(d: dict) -> None:
    try:
        CONFIG_BLOCK_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _load_config_block(name: str) -> set:
    """该 profile 阻止从父辈继承的键集合。"""
    d = _load_config_block_all()
    v = d.get(name) or []
    return set(v) if isinstance(v, list) else set()


def _ancestor_skill_dirs(name: str) -> list:
    """name 的父级一路上溯到根，收集每个祖先的 skills/ 目录绝对路径（近→远，去重防环）。
       skill 自动继承靠把这些目录塞进子的 skills.external_dirs：指向的是目录，所以 agent 把某个
       技能在目录间搬动（分类）时，继承关系自动跟着内容走，仪表盘也随磁盘实时反映——无需重连。
       性能注：hierarchy 一次加载循环复用，避免 N 次祖先 → N 次 _load_hierarchy 磁盘读。"""
    h = _load_hierarchy()
    out, seen, cur, guard = [], set(), effective_parent(name, h), 0
    while cur and cur not in seen and guard < 30:
        seen.add(cur)
        guard += 1
        out.append(str(_profile_dir(cur) / "skills"))
        cur = effective_parent(cur, h)
    return out


def _effective_config(name: str, cache: dict = None) -> dict:
    """有效 config = 沿组织树从 default 合下来的可继承键 + 自身覆盖（子 wins）。只算内存。"""
    cache = {} if cache is None else cache
    if name in cache:
        return cache[name]
    cache[name] = {}  # 占位防环
    if name == MAIN_AGENT:
        eff = _read_config(name)                       # 根：自身即有效
    elif is_main_twin(name):
        eff = _effective_config(MAIN_AGENT, cache)     # 分身 = 镜像主 agent
    else:
        p_eff = _effective_config(effective_parent(name) or MAIN_AGENT, cache)
        eff = dict(_own_config(name))                  # 自有优先（子 wins）
        for k in _INHERITABLE_KEYS:                    # 注意：这里不应用 block——block 只挡 materialize 时写盘，
            if k not in eff and k in p_eff:            # 不影响"作为父级被孙辈看到"的值；否则 X block K 会牵连 X 的整个子树。
                eff[k] = copy.deepcopy(p_eff[k])
    cache[name] = eff
    return eff


def _materialize_config(name: str, cache: dict, dry_run: bool = False):
    """把 name 的有效 config 物化进它的 config.yaml（首次改前备份、幂等）。返回 diff 或 None。"""
    if name == MAIN_AGENT or is_main_twin(name):
        return None
    cfgp = _profile_dir(name) / "config.yaml"
    if cfgp.is_symlink():                              # 已 symlink 共享，不物化
        return None
    own = _own_config(name)
    p_eff = _effective_config(effective_parent(name) or MAIN_AGENT, cache)
    block = _load_config_block(name)  # 用户在落位弹窗里取消勾选的键，不从父辈继承
    new, track = dict(own), {}
    for k in _INHERITABLE_KEYS:
        if k in block:
            continue
        if k not in own and k in p_eff:
            new[k] = copy.deepcopy(p_eff[k])
            track[k] = new[k]
    # skill 自动沿树继承：把祖先 skills 目录写进 skills.external_dirs（指向目录而非逐个 symlink ——
    # agent 之后把技能在目录间搬动/分类，继承自动跟着走、无需重连、不断链）。
    # 【默认继承】子 agent 一律默认从祖先继承技能（不再要求 .no-bundled-skills 标记）——根治
    # "raw-CLI / 新建 agent 静默成孤岛、不继承 X"那类 bug。唯一例外 = 显式 opt-out（skill_inherit_off）。
    # 物理副本去冗余（剥掉与祖先逐字节相同的 bundled 副本）由 _converge_inheritance 在生命周期事件
    # （新建 / fork / hermes 更新后）做，与本函数解耦——本函数在后台 tick 每几秒跑、绝不搬文件。
    # .no-bundled-skills 标记降级为"已去冗余"的状态记录（不再是继承门控）。
    # 计算"期望的 external_dirs"= 开则[祖先目录 + 自有额外]，关则[只剩自有额外]——后者让 opt-out 时
    # 自动把祖先目录摘掉。只在与现状不同才改（幂等）。
    skill_on = name not in _load_skill_inherit_off()
    anc = _ancestor_skill_dirs(name)
    cur_sk = new.get("skills") or {}
    cur_ext = cur_sk.get("external_dirs") or []
    own_ext = [e for e in cur_ext if e not in anc]
    desired_ext = (anc + own_ext) if skill_on else own_ext
    if desired_ext != cur_ext:
        sk = dict(cur_sk)
        if desired_ext:
            sk["external_dirs"] = desired_ext
        else:
            sk.pop("external_dirs", None)
        new["skills"] = sk
    cur = _read_config(name)
    if new == cur:
        return None
    diff = {"profile": name,
            "inherit_add": sorted(k for k in track if k not in cur),
            "inherit_update": sorted(k for k in track if k in cur and cur.get(k) != track[k]),
            "skill_external_dirs": len(anc)}
    if not dry_run:
        bak = _profile_dir(name) / "config.yaml.dashbak"
        if not bak.is_file() and cfgp.is_file():
            try:
                bak.write_text(yaml.safe_dump(cur, allow_unicode=True, sort_keys=False), encoding="utf-8")
            except OSError:
                pass
        _write_config(name, new)
        _save_inherit_track(name, track)
    return diff


def _materialize_tree(dry_run: bool = False) -> list:
    """沿组织树自上而下（default→子→孙）物化所有 profile 的 config 继承。返回有变化的 diff。"""
    cache = {}
    by = build_tree()["nodes"]
    order, seen, queue = [], set(), [MAIN_AGENT]
    while queue:
        n = queue.pop(0)
        if n in seen or n not in by:
            continue
        seen.add(n)
        order.append(n)
        queue.extend(sorted(by[n].get("children", [])))
    diffs = []
    for n in order:
        try:
            d = _materialize_config(n, cache, dry_run=dry_run)
            if d:
                diffs.append(d)
        except Exception:  # noqa: BLE001 - 单个 profile 出错不击穿全树
            pass
    return diffs


# --------------------------------------------------------------------------- #
# 去冗余 / 转继承：profile 创建时 hermes 默认拷了一份 bundled 技能（和母集重名、独立副本），
#   这些副本会"遮蔽"继承、且改母集不传播。转继承 = 把和祖先逐字节相同的副本移走（备份，可还原）
#   + 打 .no-bundled-skills 标记 → 该 profile 改吃 external_dirs 继承（单一源、改一处全员变）。
#   只动"逐字节相同的冗余副本"；定制过的、祖先没有的（自有独占）一律保留。symlink 共享一律不碰。
# --------------------------------------------------------------------------- #
DEDUP_BACKUP_DIR = HERE / "dedup-backups"


def _dirs_identical(a: Path, b: Path) -> bool:
    """两个 skill 目录是否逐字节相同（递归比所有文件内容）。"""
    if not (a.is_dir() and b.is_dir()):
        return False
    try:
        af = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
        bf = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
        if af != bf:
            return False
        for rel in af:
            if (a / rel).read_bytes() != (b / rel).read_bytes():
                return False
        return True
    except OSError:
        return False


def _prune_empty_dirs(root: Path) -> None:
    """自底向上删掉 root 下的空目录（不删 root 本身）。用于移走技能副本后清理空类目。"""
    try:
        subs = sorted((p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()),
                      key=lambda p: len(p.parts), reverse=True)
    except OSError:
        return
    for d in subs:
        try:
            next(d.iterdir())
        except StopIteration:
            try:
                d.rmdir()
            except OSError:
                pass
        except OSError:
            pass


def _convert_to_inherit(name: str, dry_run: bool = False) -> dict:
    """把 name 转成"吃继承"：**逐个 skill** 比对——和祖先同路径、逐字节相同的冗余副本移到备份，
       打 .no-bundled-skills 标记 → 改吃 external_dirs 继承。定制过的 / 祖先没有的（自有独占）保留。
       逐 skill（非逐类目）粒度：default 给某类目加了技能时，该类目里的 bundled 副本照样被识别为冗余。"""
    if name == MAIN_AGENT or is_main_twin(name):
        return {"name": name, "error": "主 agent / 分身 不参与（根是源；分身已整目录 symlink 共享）"}
    if name != "default" and not _PROFILE_ID_RE.match(name):
        return {"name": name, "error": "非法 profile 名"}
    pdir = _profile_dir(name)
    sdir = pdir / "skills"
    anc_dirs = [Path(d) for d in _ancestor_skill_dirs(name)]
    redundant, customized, unique = [], [], []   # redundant: [(rel_str, skill_dir)]
    if sdir.is_dir():
        for md in sorted(sdir.rglob("SKILL.md")):
            s = str(md)
            if "/.hub/" in s or "/.git/" in s or "/.archive/" in s:   # .archive = curator 归档区，排除
                continue
            sk = md.parent
            try:
                rel = sk.relative_to(sdir)
            except ValueError:
                continue
            if (sdir / rel.parts[0]).is_symlink():   # 通过 symlink 共享来的，一律不碰
                continue
            match = next((ad / rel for ad in anc_dirs if (ad / rel / "SKILL.md").is_file()), None)
            if match is None:
                unique.append(str(rel))                       # 祖先没有 → 自有独占，保留
            elif _dirs_identical(sk, match):
                redundant.append((str(rel), sk))              # 与祖先逐字节相同 → 冗余，删后继承
            else:
                customized.append(str(rel))                   # 改过 → 自有定制，保留
    report = {"name": name, "redundant": [r for r, _ in redundant], "kept_customized": customized,
              "kept_unique": unique, "removed": [], "errors": [],
              "ancestors_inherited_from": [str(d) for d in anc_dirs]}
    if not dry_run and redundant:
        bdir = DEDUP_BACKUP_DIR / name
        for relstr, sk in redundant:
            try:
                dst = bdir / relstr
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.move(str(sk), str(dst))                # 移到备份（保留相对路径，可还原）
                report["removed"].append(relstr)
            except OSError as e:
                report["errors"].append(f"{relstr}: {e}")
        _prune_empty_dirs(sdir)                               # 清理被删空的类目目录
    if not dry_run:
        try:
            (pdir / ".no-bundled-skills").write_text(
                "转继承（仪表盘）：不再拷贝 bundled 技能，改从组织树祖先继承（external_dirs）。\n",
                encoding="utf-8")
            report["marker_set"] = True
        except OSError as e:
            report["errors"].append(f"marker: {e}")
        off = _load_skill_inherit_off()
        if name in off:                                       # 显式转继承 = 清除任何 opt-out
            off.discard(name); _save_skill_inherit_off(off)
        _materialize_config(name, {}, dry_run=False)          # 立即设上 external_dirs
    return report


def _revert_inherit(name: str) -> dict:
    """撤销转继承：把备份里的 skill 副本按相对路径搬回 + 去 .no-bundled-skills（external_dirs 随之摘）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        return {"name": name, "error": "非法 profile 名"}
    pdir = _profile_dir(name)
    sdir = pdir / "skills"
    bdir = DEDUP_BACKUP_DIR / name
    restored = []
    if bdir.is_dir():
        for md in sorted(bdir.rglob("SKILL.md")):
            sk = md.parent
            try:
                rel = sk.relative_to(bdir)
            except ValueError:
                continue
            dst = sdir / rel
            if not dst.exists():
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(sk), str(dst))
                    restored.append(str(rel))
                except OSError:
                    pass
        _prune_empty_dirs(bdir)
        try:
            bdir.rmdir()
        except OSError:
            pass
    marker = pdir / ".no-bundled-skills"
    if marker.exists():
        try:
            marker.unlink()
        except OSError:
            pass
    off = _load_skill_inherit_off(); off.add(name); _save_skill_inherit_off(off)  # 默认开 → opt-out 才能摘继承
    _materialize_config(name, {}, dry_run=False)              # opt-out → 摘掉祖先 external_dirs
    return {"name": name, "restored": restored}


def _converge_inheritance(dry_run: bool = False) -> list:
    """沿组织树把每个【非分身、未 opt-out】的 agent 收敛到默认继承态：剥离与祖先逐字节相同的
    bundled 副本（移到备份，可逆）。幂等——已收敛的 agent 无冗余可剥、不产生变化。
    用途（治本的"保持"环节，配合默认开门控）：
      ① 新建 / fork agent 后——hermes profile create 默认拷了一份 bundled 内置技能，剥掉改吃继承；
      ② hermes update 重新 bundle 内置技能后——重新剥离，防止物理副本与 external_dirs 继承双计。
    注意：绝不在后台 tick(_config_sync_tick) 里调用——那会每几秒搬一次文件。只挂生命周期事件。"""
    out = []
    by = build_tree()["nodes"]
    order, seen, queue = [], set(), [MAIN_AGENT]
    while queue:
        n = queue.pop(0)
        if n in seen or n not in by:
            continue
        seen.add(n); order.append(n); queue.extend(sorted(by[n].get("children", [])))
    off = _load_skill_inherit_off()
    for n in order:
        if n == MAIN_AGENT or is_main_twin(n) or n in off:    # 根/分身/显式 opt-out 不收敛
            continue
        try:
            r = _convert_to_inherit(n, dry_run=dry_run)
            n_strip = len(r.get("removed") or r.get("redundant") or [])
            if n_strip or r.get("error"):
                out.append({"name": n, "stripped": n_strip, "error": r.get("error")})
        except Exception:  # noqa: BLE001 - 单个 agent 出错不击穿全树收敛
            pass
    return out


class InheritReq(BaseModel):
    child: str
    parent: str | None = None
    keys: list[str] = []


class RevertConfigReq(BaseModel):
    child: str
    confirm: str = ""


@app.get("/api/config/{name}")
def api_config(name: str):
    """该 profile config.yaml 里可继承的顶层键（供选择）+ 是否有备份可还原。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    cfg = _read_config(name)
    return {"name": name,
            "keys": [k for k in _INHERITABLE_KEYS if k in cfg],
            "has_backup": (_profile_dir(name) / "config.yaml.dashbak").is_file()}


@app.post("/api/inherit-config")
def api_inherit_config(req: InheritReq):
    """把 parent 选定的 config.yaml 键快照合并进 child（先备份，可 revert）。
    注意：这是快照拷贝，不随 parent 改动自动更新；要实时继承请用共享(symlink)走目录类技能/MCP。"""
    if not req.parent:
        raise HTTPException(400, "缺少 parent")
    for nm in (req.child, req.parent):
        if nm != "default" and not _PROFILE_ID_RE.match(nm):
            raise HTTPException(400, f"非法 profile 名: {nm}")
    if req.child == req.parent:
        raise HTTPException(400, "child 与 parent 相同")
    pcfg = _read_config(req.parent)
    keys = [k for k in req.keys if k in _INHERITABLE_KEYS and k in pcfg]
    if not keys:
        raise HTTPException(400, "没有可继承的有效键")
    ccfg = _read_config(req.child)
    bak = _profile_dir(req.child) / "config.yaml.dashbak"
    try:
        bak.write_text(yaml.safe_dump(ccfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        merged = dict(ccfg)
        for k in keys:
            merged[k] = _merge_inherited_value(ccfg.get(k), pcfg[k])
        _write_config(req.child, merged)
    except OSError as e:
        raise HTTPException(500, f"写入失败: {e}")
    return {"ok": True, "child": req.child, "parent": req.parent, "inherited": keys,
            "note": "快照合并（dict 深合并 / list 并集，保留子级自有项）；如需随父级实时更新，请用共享(symlink)继承目录类技能/MCP"}


@app.post("/api/revert-config")
def api_revert_config(req: RevertConfigReq):
    """还原 child 的 config.yaml 到继承前的备份。"""
    if req.child != "default" and not _PROFILE_ID_RE.match(req.child):
        raise HTTPException(400, "非法 profile 名")
    if req.confirm != req.child:
        raise HTTPException(400, "必须用目标 profile 名确认还原")
    bak = _profile_dir(req.child) / "config.yaml.dashbak"
    if not bak.is_file():
        raise HTTPException(404, "无备份可还原")
    try:
        restored = yaml.safe_load(bak.read_text(encoding="utf-8")) or {}
        if not isinstance(restored, dict):
            raise HTTPException(409, "备份内容不是有效配置对象")
        _write_config(req.child, restored)
        bak.unlink()
    except (OSError, yaml.YAMLError) as e:
        raise HTTPException(500, f"还原失败: {e}")
    return {"ok": True, "child": req.child}


# --------------------------------------------------------------------------- #
# config 沿组织树自动物化（后台 tick）：_config_sync_tick 周期性跑 _materialize_tree，
#   让子 agent 的可继承键自动跟随父级。hermes 无原生跨 profile config 继承，这是仪表盘加的层。
#   注意：物化写到磁盘——正在跑的会话需重开才生效。_SYNC_LOCK 串行化所有 config 写。
#   （旧的逐边 opt-in 实时同步 config_inherit.json / _sync_one 已被组织树物化取代，2026-06 删除。）
# --------------------------------------------------------------------------- #
_SYNC_LOCK = threading.RLock()   # 串行化 config 写 + tick；RLock 允许 tick→_write_config 同线程重入


_last_cfg_sig = None  # 上次物化时的 config 文件指纹（mtime）


def _config_files_sig() -> tuple:
    """所有 config.yaml + 继承标记文件的 mtime 指纹。没变 = 没必要重算继承物化。"""
    paths = [HERMES_HOME / "config.yaml"]
    try:
        for d in PROFILES_DIR.iterdir():
            if d.is_dir():
                paths.append(d / "config.yaml")
                paths.append(d / ".dash_inherited.json")
    except OSError:
        pass
    sig = []
    for p in paths:
        try:
            sig.append((str(p), p.stat().st_mtime_ns))
        except OSError:
            pass
    return tuple(sorted(sig))


def _config_sync_tick(force: bool = False) -> None:
    """后台 tick：沿组织树自动物化 config 继承。幂等——收敛后无变化就不写盘。
       脏检查：config 文件 mtime 没变就跳过那 ~1s 的全树重算(稳态≈零开销),改动才跑。"""
    global _last_cfg_sig
    with _SYNC_LOCK:
        sig = _config_files_sig()
        if not force and sig == _last_cfg_sig:
            return  # 配置未变,跳过昂贵物化
        try:
            _materialize_tree(dry_run=False)
        except Exception:  # noqa: BLE001 - 同步永不击穿
            pass
        _last_cfg_sig = _config_files_sig()  # 物化可能写了 config → 以写后状态为基准


# --------------------------------------------------------------------------- #
# 用量回流聚合（curator 适配策略 E）：curator 判"陈旧/归档"只读本 agent 的 .usage.json，
#   看不见子 agent 通过继承(external_dirs)在用 X 的技能 → 会误归档"X 自己不用、但下游热"的
#   共享技能（已发生过：hermes-skill-management 被并掉）。修法：定期把各子 agent .usage.json 里
#   每个技能的 last_used_at 取最大值，回填到 X 的 .usage.json。只抬高 last_used_at（只会让
#   curator 更晚归档 = 安全方向，绝不会造成误删），绝不动技能本体。net-zero、脏检查、可关。
# --------------------------------------------------------------------------- #
_USAGE_AGG_ENABLED = True
_last_usage_sig = None


def _usage_files_sig() -> tuple:
    """所有 agent 的 skills/.usage.json 的 mtime 指纹。没变 = 没必要重算聚合。"""
    sig = []
    for nm in _all_profile_names():
        p = _profile_dir(nm) / "skills" / ".usage.json"
        try:
            sig.append((str(p), p.stat().st_mtime_ns))
        except OSError:
            pass
    return tuple(sorted(sig))


def _aggregate_usage_into_main(dry_run: bool = False) -> dict:
    """把各子 agent 的技能 last_used_at 回流到主 agent(X)，让 X 的 curator 看见下游用量。
       仅更新 X 已有条目、且仅当下游时间更新时（ISO-8601 UTC 字符串可直接比较）。"""
    xpath = _profile_dir(MAIN_AGENT) / "skills" / ".usage.json"
    if not xpath.is_file():
        return {"updated": [], "downstream_skills": 0, "reason": "X 无 .usage.json", "dry_run": dry_run}
    try:
        x_usage = json.loads(xpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"updated": [], "downstream_skills": 0, "reason": "X .usage.json 解析失败", "dry_run": dry_run}
    if not isinstance(x_usage, dict):
        return {"updated": [], "downstream_skills": 0, "reason": "格式异常", "dry_run": dry_run}
    # 收集各子 agent 对每个技能名的最新 last_used_at
    downstream: dict = {}
    for nm in _all_profile_names():
        if nm == MAIN_AGENT:
            continue
        try:
            sub = json.loads((_profile_dir(nm) / "skills" / ".usage.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(sub, dict):
            continue
        for name, rec in sub.items():
            if not isinstance(rec, dict):
                continue
            lu = rec.get("last_used_at")
            if lu and (name not in downstream or str(lu) > downstream[name]):
                downstream[name] = str(lu)
    # 回填:只抬高 X 已有条目的 last_used_at
    updated = []
    for name, rec in x_usage.items():
        if not isinstance(rec, dict):
            continue
        d = downstream.get(name)
        if d and str(d) > str(rec.get("last_used_at") or ""):
            updated.append({"skill": name, "from": rec.get("last_used_at"), "to": d})
            if not dry_run:
                rec["last_used_at"] = d
    if updated and not dry_run:
        try:
            tmp = xpath.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(x_usage, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, xpath)
        except OSError:
            pass
    return {"updated": updated, "downstream_skills": len(downstream), "dry_run": dry_run}


def _usage_agg_tick(force: bool = False) -> None:
    """后台 tick：脏检查 → 用量回流。稳态(各 .usage.json 没变)≈零开销。永不击穿。"""
    global _last_usage_sig
    if not _USAGE_AGG_ENABLED:
        return
    sig = _usage_files_sig()
    if not force and sig == _last_usage_sig:
        return
    try:
        _aggregate_usage_into_main(dry_run=False)
    except Exception:  # noqa: BLE001
        pass
    _last_usage_sig = _usage_files_sig()


# --------------------------------------------------------------------------- #
# 更新自动侦测（update-health）：监测 Hermes 内核 git HEAD —— 变了 = 发生过 `hermes update`。
#   一变就自动跑 selfcheck.py(确定性、只读巡检,不需要任何代码 skill),把绿/红落盘供 UI 弹窗。
#   = 检测全自动；修复仍由人触发 dev 的 update-guard(改代码必须人把关)。
# --------------------------------------------------------------------------- #
_UPDATE_WATCH = HERE / ".update_watch.json"


def _hermes_git_head() -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(HERMES_HOME / "hermes-agent"), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _load_update_watch() -> dict:
    try:
        return json.loads(_UPDATE_WATCH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _run_selfcheck_capture() -> dict:
    """跑 selfcheck.py(确定性体检)→ {ok, failures[], output}。用 server 自己的解释器(必有依赖)。"""
    sc = HERE / "selfcheck.py"
    if not sc.is_file():
        return {"ok": True, "failures": [], "output": "selfcheck.py 不存在", "skipped": True}
    try:
        r = subprocess.run([sys.executable, str(sc)], capture_output=True, text=True, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        fails = [ln.split("❌", 1)[1].strip() for ln in out.splitlines() if ln.strip().startswith("❌")]
        return {"ok": r.returncode == 0, "failures": fails, "output": out[-4000:]}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": True, "failures": [], "output": f"selfcheck 未能运行: {e}", "skipped": True}


def _update_health_tick(force: bool = False) -> dict:
    """侦测 git HEAD 变化 → 变了(或 force)就自动 selfcheck 落盘。没变则零开销(只一次 git 调用)。"""
    head = _hermes_git_head()
    prev = _load_update_watch()
    changed = bool(head) and head != prev.get("head")
    if not force and not changed and prev:
        return prev
    res = _run_selfcheck_capture()
    state = {"head": head, "version": prev.get("version"), "checked_at": int(time.time()),
             "changed_since_last": changed, "prev_head": prev.get("head"),
             "ok": res["ok"], "failures": res["failures"], "output": res.get("output", "")}
    try:
        _rc, ver, _e = run_hermes(["version"], timeout=10)
        if ver:
            state["version"] = ver.strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        pass
    try:
        tmp = _UPDATE_WATCH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _UPDATE_WATCH)
    except OSError:
        pass
    return state


_INDEX_SIG = {"v": None}


def _index_sync_tick(force: bool = False) -> None:
    """上下文索引 INDEX.md 维护（docs/context-index-architecture.md Phase 1）。
    结构签名(纯 stat)脏检查,变了才重建;任何异常不击穿 tick。生命周期事件
    (建/删/改名/移树)都会改 hierarchy/labels/SOUL 的 mtime,天然被签名捕获。"""
    try:
        import index_builder as _ib
        sig = _ib.signature(HERMES_HOME)
        if force or sig != _INDEX_SIG["v"]:
            _ib.write_index(HERMES_HOME)
            _INDEX_SIG["v"] = sig
    except Exception:  # noqa: BLE001 - 索引维护永不击穿 tick
        pass


def _config_sync_loop(interval: int = 5) -> None:
    # Curator 安全不变式:开机先校正一次(覆盖"hermes update 把 prune_builtins 默认翻成 True")。
    try:
        _enforce_curator_safety(dry_run=False)
    except Exception:  # noqa: BLE001 - 安全校正永不击穿
        pass
    # 更新技能维护:开机先清一次(覆盖"刚 hermes update 完就重启后端"),之后约每 60s 清一次回灌的
    #   黑名单技能。X 未冻结(update 照常带新技能进),靠这个常驻 tick 保证"删的/搬走的不复活"。
    try:
        _run_skill_maintenance(dry_run=False)
    except Exception:  # noqa: BLE001
        pass
    _usage_agg_tick(force=True)  # 开机先把下游用量回流一次(curator 适配策略 E)
    try:
        _update_health_tick(force=True)  # 开机先建一次基线(覆盖"刚 update 完就重启后端")
    except Exception:  # noqa: BLE001
        pass
    try:
        _constitution_sync_tick(force=True)  # 开机先重刷一次,覆盖后端停机期间手改 constitution.md
    except Exception:  # noqa: BLE001
        pass
    _index_sync_tick(force=True)  # 开机先重建一次上下文索引(覆盖停机期间的组织/记忆变化)
    n = 0
    while True:
        _config_sync_tick()
        try:
            _constitution_sync_tick()
        except Exception:  # noqa: BLE001 - 宪法同步永不击穿 tick
            pass
        _index_sync_tick()  # 签名脏检查(纯 stat,~75 个文件);结构没变=零开销
        n += 1
        if n % 12 == 0:  # ~60s 一次
            try:
                _run_skill_maintenance(dry_run=False)
            except Exception:  # noqa: BLE001 - 维护永不击穿 tick
                pass
            try:
                _enforce_curator_safety(dry_run=False)  # ~60s 校正 curator 安全不变式
            except Exception:  # noqa: BLE001 - 安全校正永不击穿 tick
                pass
        if n % 720 == 0:  # ~1h 一次:用量回流(脏检查,没变就跳过;curator 7d 才巡,不用很新)
            _usage_agg_tick()
        if n % 60 == 0:  # ~5min 一次:侦测 Hermes 版本变化(git HEAD 没变=零开销;变了才自动 selfcheck)
            try:
                _update_health_tick()
            except Exception:  # noqa: BLE001 - 侦测永不击穿 tick
                pass
        time.sleep(interval)


@app.post("/api/index/rebuild")
def api_index_rebuild():
    """手动重建上下文索引 INDEX.md（正常情况下由 _index_sync_tick 自动维护）。"""
    import index_builder as _ib
    p = _ib.write_index(HERMES_HOME)
    _INDEX_SIG["v"] = _ib.signature(HERMES_HOME)
    return {"ok": True, "path": str(p)}


# （旧的 config-inherit 逐边实时同步端点 list/enable/disable + CfgInheritReq 已于 2026-06 删除：
#   组织树自动物化 _materialize_tree 已取代逐边 opt-in 同步，前端也不再调用这些端点。）


@app.get("/api/config-tree")
def api_config_tree(dry_run: bool = True):
    """沿组织树的 config 继承现状。dry_run=true（默认）只算 diff 不写盘，供查看"谁继承了哪些键"。
       后台 tick 已在自动物化；这个端点用于按需查看 / 触发一次。"""
    diffs = _materialize_tree(dry_run=bool(dry_run))
    return {"dry_run": bool(dry_run), "inheritable_keys": _INHERITABLE_KEYS, "changes": diffs}


# --------------------------------------------------------------------------- #
# 落位预演：拖动改父级前先算"完整 profile diff"，让前端 tab 化对话框逐项展示，
#   用户取消则什么都没发生；点确认才走 /api/move + /api/config-tree 物化。
# --------------------------------------------------------------------------- #
class ReparentPreviewReq(BaseModel):
    node: str
    new_parent: str | None = None


def _summarize_value(v) -> str:
    """简短描述 config 值，避免在 diff JSON 里塞巨大原值（dict/list 只给条数，标量截断）。"""
    if isinstance(v, dict):
        return f"dict · {len(v)} 项"
    if isinstance(v, list):
        return f"list · {len(v)} 项"
    s = str(v)
    return s if len(s) <= 80 else (s[:77] + "...")


def _ancestor_chain(name: str, h: dict) -> list:
    """name 在层级 h 下的祖先名序列（近→远，去重防环）。供预演用，不影响真实树。"""
    out, seen, cur, guard = [], set(), effective_parent(name, h), 0
    while cur and cur not in seen and guard < 30:
        seen.add(cur)
        guard += 1
        out.append(cur)
        cur = effective_parent(cur, h)
    return out


def _ancestor_skill_dirs_h(name: str, h: dict) -> list:
    """祖先 skills/ 目录序列（按层级 h 计算，预演用）。"""
    return [str(_profile_dir(n) / "skills") for n in _ancestor_chain(name, h)]


def _effective_inherited_with(name: str, h: dict, cache: dict) -> dict:
    """在假定层级 h 下，name 会从父辈继承到的键值映射（不含自有键、不含 block 列表里的键）。
       父级以上的层级未变，所以父级的有效 config 算法和实树一致。"""
    if name in cache:
        return cache[name]
    cache[name] = {}  # 占位防环
    if name == MAIN_AGENT or is_main_twin(name):
        return {}
    par = effective_parent(name, h) or MAIN_AGENT
    if par == MAIN_AGENT or is_main_twin(par):
        p_eff = _read_config(MAIN_AGENT)  # 分身 = 镜像主 agent
    else:
        par_inh = _effective_inherited_with(par, h, cache)
        par_own = _own_config(par)
        p_eff = dict(par_own)
        for k, v in par_inh.items():
            p_eff.setdefault(k, v)
    own = _own_config(name)
    block = _load_config_block(name)
    res = {}
    for k in _INHERITABLE_KEYS:
        if k in block:
            continue
        if k in p_eff and k not in own:
            res[k] = copy.deepcopy(p_eff[k])
    cache[name] = res
    return res


@app.post("/api/reparent-preview")
def api_reparent_preview(req: ReparentPreviewReq):
    """落位预演：不写盘，返回 node 移到 new_parent 下后的完整 profile diff
       （祖先链 / external_dirs / config 继承键增删改 / 宪法订阅状态 / 警告）。
       前端拖动落位后拿它驱动确认对话框；用户取消则什么都没发生。"""
    node = req.node
    if node != "default" and not _PROFILE_ID_RE.match(node):
        raise HTTPException(400, "非法 node 名")
    if not _profile_dir(node).is_dir():
        raise HTTPException(404, "node 不存在")
    np = req.new_parent or None
    if np is not None:
        if np != "default" and not _PROFILE_ID_RE.match(np):
            raise HTTPException(400, "非法 new_parent 名")
        if not _profile_dir(np).is_dir():
            raise HTTPException(404, "new_parent 不存在")
        if np == node:
            raise HTTPException(400, "不能把自己设为自己的父级")
        if np in _descendants(node, build_tree()["nodes"]):
            raise HTTPException(409, "会产生循环引用（new_parent 在 node 的子树里）")
    h_real = _load_hierarchy()
    h_sim = dict(h_real)
    h_sim[node] = np
    # 祖先链。注意：node 若是草稿，effective_parent 永远返回 None，所以"落位"链路要从 np 直接起算，
    # 否则 anc_after 会被锁死成 []。anc_before 对草稿来说本就是 []（草稿无父），保持现有逻辑。
    anc_before = _ancestor_chain(node, h_real)
    def _chain_from(start, h):
        out, seen, cur, guard = [], set(), start, 0
        while cur and cur not in seen and guard < 30:
            seen.add(cur); guard += 1; out.append(cur)
            cur = effective_parent(cur, h)
        return out
    anc_after = _chain_from(np, h_sim) if np else _ancestor_chain(node, h_sim)
    # 默认继承：除非显式 opt-out，落位都会重挂祖先 external_dirs（与 _materialize_config 同口径）
    converted = (_profile_dir(node) / ".no-bundled-skills").exists()  # 仅供前端显示"已精简"
    skill_inherit_on = node not in _load_skill_inherit_off()
    anc_dirs_before = _ancestor_skill_dirs_h(node, h_real) if skill_inherit_on else []
    anc_dirs_after = _ancestor_skill_dirs_h(node, h_sim) if skill_inherit_on else []
    ext_add = [d for d in anc_dirs_after if d not in anc_dirs_before]
    ext_remove = [d for d in anc_dirs_before if d not in anc_dirs_after]
    # config 继承键 diff
    twin = is_main_twin(node)
    apex = (node == MAIN_AGENT)
    inh_before = {} if (apex or twin) else _effective_inherited_with(node, h_real, {})
    inh_after = {} if (apex or twin) else _effective_inherited_with(node, h_sim, {})
    added = sorted(k for k in inh_after if k not in inh_before)
    removed = sorted(k for k in inh_before if k not in inh_after)
    value_changed = sorted(k for k in inh_before if k in inh_after and inh_before[k] != inh_after[k])
    own_keys = sorted(_own_config(node).keys())
    items = []
    for k in added:
        items.append({"key": k, "change": "add", "after": _summarize_value(inh_after[k])})
    for k in removed:
        items.append({"key": k, "change": "remove", "before": _summarize_value(inh_before[k])})
    for k in value_changed:
        items.append({"key": k, "change": "update",
                      "before": _summarize_value(inh_before[k]),
                      "after": _summarize_value(inh_after[k])})
    # 宪法订阅状态（落位不会自动改它，由弹窗里用户选）
    const_subscribed = node in _load_const_subs()
    # 后代节点数（其继承链也会随之挪动）
    descendants = sorted(_descendants(node, build_tree()["nodes"]))
    warnings = []
    if twin:
        warnings.append("该 agent 是主 agent 的分身（config/skills/memories 整份镜像 default）——落位不会改变它的继承，主 agent 才是它的源。")
    if apex:
        warnings.append("default 是主 agent / 树根，移动它在仪表盘语义里没有意义。")
    if descendants:
        warnings.append(f"该 agent 有 {len(descendants)} 个后代节点会跟着挪到新位置，它们的继承链路也会随之变更。")
    return {
        "node": node,
        "from": {"parent": effective_parent(node, h_real), "ancestors": anc_before},
        "to":   {"parent": np, "ancestors": anc_after},
        "config_diff": {
            "items": items,
            "own_keys_unchanged": own_keys,
            "inheritable_keys": list(_INHERITABLE_KEYS),
        },
        "skill_diff": {
            "skill_inherit_on": skill_inherit_on,
            "converted": converted,
            "ext_dirs_before": anc_dirs_before,
            "ext_dirs_after": anc_dirs_after,
            "ext_dirs_add": ext_add,
            "ext_dirs_remove": ext_remove,
        },
        "constitution": {"subscribed": const_subscribed},
        "descendants": descendants,
        "is_main_twin": twin,
        "warnings": warnings,
    }


@app.get("/api/skills/convert-inherit/{name}")
def api_convert_inherit_preview(name: str):
    """预览：把 name 转继承会删掉哪些冗余副本、保留哪些（只读 dry-run）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    return _convert_to_inherit(name, dry_run=True)


@app.post("/api/skills/convert-inherit/{name}")
def api_convert_inherit(name: str):
    """执行转继承：删与祖先相同的冗余 bundled 副本（移到备份，可还原）+ 打标记 → 改吃继承。
    ⚠ 破坏性（移除技能副本，但已备份且与祖先相同 = 继承后等价）；前端须二次确认。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    r = _convert_to_inherit(name, dry_run=False)
    if r.get("error"):
        raise HTTPException(409, r["error"])
    return {"ok": True, **r}


@app.post("/api/skills/revert-inherit/{name}")
def api_revert_inherit(name: str):
    """撤销转继承：备份里的副本搬回 + 去标记 + opt-out（external_dirs 随之摘掉）。可逆。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    return {"ok": True, **_revert_inherit(name)}


@app.post("/api/skills/converge-all")
def api_converge_all(dry_run: int = 0):
    """全树收敛默认继承：剥离各 agent 与祖先逐字节相同的 bundled 副本（可逆，移备份）。
    主要用于 hermes update 重新 bundle 内置技能后的再剥离（防双计）。dry_run=1 只预览不动盘。"""
    return {"ok": True, "dry_run": bool(dry_run), "converged": _converge_inheritance(dry_run=bool(dry_run))}


# --------------------------------------------------------------------------- #
# 更新技能维护：X(default) 未打 .no-bundled-skills 标记 → hermes update 会按出厂清单
# 回灌内置技能。这里维护一份「黑名单」(dashboard/skill-blocklist.json，路径制)，更新后把
# 回灌的黑名单技能从 skills/<path> 再移除一遍 —— 既保留"删的不回来/搬仓库的不复活",又让
# update 照常带进新技能(X 不冻结)。三态:使用中(在 skills/)/仓库(.warehouse 有副本)/已删。
# --------------------------------------------------------------------------- #
_SKILL_BLOCKLIST = HERE / "skill-blocklist.json"


def _load_skill_blocklist() -> list:
    try:
        d = json.loads(_SKILL_BLOCKLIST.read_text(encoding="utf-8"))
        return [e for e in (d.get("remove_paths") or []) if isinstance(e, dict) and e.get("path")]
    except (OSError, ValueError):
        return []


def _run_skill_maintenance(dry_run: bool = True) -> dict:
    """把黑名单里的出厂技能从 X 的 skills/ 移除(防 update 回灌)。dry_run=只报告不动盘。"""
    skills_root = (HERMES_HOME / "skills").resolve()
    entries = _load_skill_blocklist()
    present, removed = [], []
    for e in entries:
        rel = str(e["path"]).strip().strip("/")
        if not rel or rel.startswith(".."):
            continue
        target = (skills_root / rel)
        try:  # 越界防护：必须落在 skills_root 内
            target.resolve().relative_to(skills_root)
        except (ValueError, OSError):
            continue
        if not target.exists():
            continue
        present.append({"path": rel, "name": e.get("name", rel), "state": e.get("state")})
        if not dry_run:
            try:
                shutil.rmtree(target) if target.is_dir() else target.unlink()
                removed.append(rel)
            except OSError:
                pass
    return {"blocklist_count": len(entries), "present": present, "removed": removed, "dry_run": dry_run}


# --------------------------------------------------------------------------- #
# Curator 安全不变式:防止 hermes 把 curator.prune_builtins 的默认值翻成 True。
#   背景:hermes-agent 在某次升级里把 tools/skill_usage.py 的默认从 False→True,
#   导致 X 上一批出厂(bundled)技能被 curator 的 LLM consolidation pass 当"低价值"
#   一口气裁掉(2026-06-14 一次裁了 25 个,可恢复但是凶险)。
#   解决方案:dashboard 把"X 的 curator.prune_builtins 必须为 false"做成不变式,
#   开机一次 + 每 ~60s 一次自动校正。这样:
#     - 不管 hermes 怎么升级、怎么改默认,只要 dashboard 在跑就强制 false;
#     - 不变式在 dashboard 架构里,不在 hermes 代码里,不随 `hermes update` 失效;
#     - 仪表盘上线给别人用:他们启动 dashboard 就自动安全,不需要手动改 config。
#   配置:_CURATOR_SAFE_DEFAULTS 在 X 的 curator 段没显式覆盖时,会被强制写入。
# --------------------------------------------------------------------------- #
_CURATOR_SAFE_DEFAULTS = {
    "prune_builtins": False,   # 出厂(.bundled_manifest 内)技能绝不被 curator 裁
}


def _enforce_curator_safety(dry_run: bool = False) -> dict:
    """全树校正 curator 安全不变式(无白名单/无例外,符合全局一致性):
       - X(MAIN_AGENT)总是显式写安全默认值 → 经组织树继承传播到所有继承它的 agent;
       - 任何【自有】了 curator 段、且其安全键与默认值不符的子 agent 也就地校正
         (child-wins 下,只有自有副本会绕过 X 的继承,所以必须逐个堵)。
       只读自有 config 判定(`_own_config`),纯继承的 agent 没有自有 curator 键 → 不写、零开销。
       分身 config.yaml 是 symlink→X,跳过(避免经 symlink 重复写 X)。走 _write_config(原子+锁)。"""
    changed = []
    for name in _all_profile_names():
        if name != MAIN_AGENT and is_main_twin(name):
            continue
        # X 永远校正;子 agent 仅当它【自有】curator 段(物理值≠继承账本)时才需要逐个堵
        own = _own_config(name)
        if name != MAIN_AGENT and "curator" not in own:
            continue
        cfg = _read_config(name)
        cur = cfg.get("curator")
        if not isinstance(cur, dict):
            if name == MAIN_AGENT:        # X 没有 curator 段也要建一个安全的
                cur = {}
            else:
                continue
        local = []
        for k, want in _CURATOR_SAFE_DEFAULTS.items():
            if cur.get(k) != want:
                local.append({"key": k, "from": cur.get(k, "(unset)"), "to": want})
                cur[k] = want
        if local:
            changed.append({"agent": name, "fixes": local})
            if not dry_run:
                cfg["curator"] = cur
                _write_config(name, cfg)
    return {"changed": changed, "dry_run": dry_run, "safe_defaults": _CURATOR_SAFE_DEFAULTS}


@app.get("/api/curator/safety")
def api_curator_safety_preview():
    """预览:X 的 curator 段当前值 vs 安全不变式,看哪些键会被校正。"""
    return _enforce_curator_safety(dry_run=True)


@app.post("/api/curator/safety")
def api_curator_safety_run():
    """立即执行一次校正(后台 tick 已每 ~60s 自动跑,这个端点供手动触发/调试)。"""
    return _enforce_curator_safety(dry_run=False)


@app.get("/api/skills/maintenance")
def api_skill_maintenance_preview():
    """预览：黑名单里有哪些当前真出现在 X 的 skills/(= update 回灌了、待清)。"""
    return _run_skill_maintenance(dry_run=True)


@app.post("/api/skills/maintenance")
def api_skill_maintenance_run():
    """执行更新技能维护:清掉回灌的黑名单技能(仓库副本/已删状态不受影响)。"""
    return _run_skill_maintenance(dry_run=False)


@app.get("/api/update-health")
def api_update_health(run: bool = False):
    """更新自动侦测结果。后台已在 Hermes 版本变化时自动 selfcheck;run=true 强制立即体检一次。
       UI 据此弹"更新后体检"红卡(绿则前端零占位)。"""
    # 非 run:做一次廉价 git HEAD 比对(没变=只一次 git 调用,零开销;变了才自动 selfcheck)。
    return _update_health_tick(force=bool(run))


@app.post("/api/update-health/run")
def api_update_health_run():
    """显式触发一次更新后体检；保留 GET run=true 仅作旧客户端兼容。"""
    return _update_health_tick(force=True)


@app.get("/api/skills/usage-aggregation")
def api_usage_aggregation(run: bool = False):
    """curator 适配策略 E:查看/触发"下游用量回流到 X"。
       默认 dry_run(只看哪些技能会被抬高 last_used_at);run=true 真写回。后台已每~1h 自动跑。"""
    return _aggregate_usage_into_main(dry_run=not bool(run))


@app.get("/api/skills/blocklist")
def api_skill_blocklist():
    """看黑名单全文(三态:仓库/已删),供前端 UI 展示。"""
    try:
        return json.loads(_SKILL_BLOCKLIST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"remove_paths": []}


# --------------------------------------------------------------------------- #
# 规范体检（lint）：只读扫描 skills frontmatter + config.yaml:mcp_servers，按硬/软违规列出。
#   依据 conventions：标识符用 hyphen-case；skill 必备 name + description（hermes 渐进式披露靠
#   description 检索）。只校验 hermes 真正用到的核心字段，不强制 scope/inheritable 等编排层扩展
#   字段（那套对个人单机系统过重）。继承来的 symlink skill 归源 profile 查，避免重复报。
# --------------------------------------------------------------------------- #
_HYPHEN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")        # 规范：小写+数字+连字符
_UNDERSCORE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")   # 仅多了下划线（软违规：建议连字符）


def _lint_name(kind: str, name: str) -> list:
    """标识符字符集体检。返回 [(level, msg)]：hard=非法字符；soft=用了下划线（建议 hyphen-case）。"""
    if not name or _HYPHEN_NAME_RE.match(name):
        return []
    if _UNDERSCORE_NAME_RE.match(name):
        return [("soft", f"{kind} 名 “{name}” 含下划线，建议连字符（hermes / agentskills.io 用 hyphen-case）")]
    return [("hard", f"{kind} 名 “{name}” 含非法字符（仅允许小写字母、数字、连字符）")]


def lint_profile(name: str) -> dict:
    """只读规范体检：扫该 profile 的自有 skill frontmatter + config.yaml:mcp_servers。"""
    hard, soft = [], []

    def add(level, where, msg):
        (hard if level == "hard" else soft).append({"where": where, "msg": msg})

    sdir = _profile_dir(name) / "skills"
    for md, category, via_link in _iter_skill_mds(sdir):
        if via_link:
            continue  # 继承来的（symlink）由源 profile 负责
        try:
            rel = md.relative_to(sdir)
            where = f"skills/{rel}"
        except ValueError:
            where = f"skills/{category}/{md.parent.name}"
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm_name, desc = parse_frontmatter(text)
        if not fm_name:
            add("hard", where, "SKILL.md 缺 name 字段")
        else:
            for level, msg in _lint_name("skill", fm_name):
                add(level, where, msg)
        desc = (desc or "").strip()
        if not desc:
            add("hard", where, "SKILL.md 缺 description（hermes 渐进式披露靠它检索，缺了几乎不会被触发）")
        elif len(desc) < 30:
            add("soft", where, "description 过短（<30 字符），触发命中率低")

    cfg = _read_config(name)
    mcp = cfg.get("mcp_servers")
    if isinstance(mcp, dict):
        for sname, sval in mcp.items():
            where = f"config.yaml:mcp_servers/{sname}"
            for level, msg in _lint_name("mcp_servers", str(sname)):
                add(level, where, msg)
            if not isinstance(sval, dict) or not sval:
                add("soft", where, "条目为空或非映射（应含连接配置，如 command / url / transport）")

    return {"name": name, "hard": hard, "soft": soft,
            "hard_count": len(hard), "soft_count": len(soft), "ok": not hard}


@app.get("/api/lint/{name}")
def api_lint_one(name: str):
    """单个 profile 的规范体检（只读）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    return lint_profile(name)


@app.get("/api/lint")
def api_lint_all():
    """全网规范体检（只读）：逐 profile 汇总硬/软违规。"""
    reports = [lint_profile(n) for n in _all_profile_names()]
    return {
        "profiles": reports,
        "total_hard": sum(r["hard_count"] for r in reports),
        "total_soft": sum(r["soft_count"] for r in reports),
    }


# --------------------------------------------------------------------------- #
# 仪表盘 summary：3 张卡（系统健康 / 待你 Review / 最近变更）的一次性聚合。
# 全部走"读盘 + 调内部 helper"，不解析 PTY 输出，纯只读。
# --------------------------------------------------------------------------- #
def _dashboard_summary() -> dict:
    rows, _, _ = build_profiles()
    h = _load_hierarchy()
    ks = _load_killed()
    drafts_set = _load_drafts()
    const_subs = _load_const_subs()

    # ---- 系统健康 ----
    n_total, n_main, n_twin, n_sub, n_drafts = 0, 0, 0, 0, 0
    n_killed, n_eff_killed = 0, 0
    for r in rows:
        nm = r["name"]
        if nm in drafts_set:
            n_drafts += 1
            continue
        n_total += 1
        if nm == MAIN_AGENT:
            n_main += 1
        elif is_main_twin(nm):
            n_twin += 1
        else:
            n_sub += 1
        if nm in ks:
            n_killed += 1
        if effective_killed(nm, ks, h):
            n_eff_killed += 1
    # 分身整份 skills/ 是 symlink 到 default 的，lint 出来的违规和 default 同一份，重复计数没意义→排除
    lint_reports = [lint_profile(n) for n in _all_profile_names() if not is_main_twin(n)]
    total_hard = sum(r["hard_count"] for r in lint_reports)
    total_soft = sum(r["soft_count"] for r in lint_reports)
    default_mcp = list((_read_config(MAIN_AGENT).get("mcp_servers") or {}).keys())
    agents_with_mcp = 0
    cache = {}
    for r in rows:
        nm = r["name"]
        if nm in drafts_set or nm == MAIN_AGENT:
            continue
        if (_effective_config(nm, cache).get("mcp_servers") or {}):
            agents_with_mcp += 1
    n_subbed = sum(1 for nm in const_subs if _profile_dir(nm).is_dir())

    # ---- 待你 Review ----
    hard_list = [
        {"profile": r["name"], "items": r["hard"][:5], "count": r["hard_count"]}
        for r in lint_reports if r["hard_count"] > 0
    ]
    hard_list.sort(key=lambda x: -x["count"])

    # ---- 最近变更（时间倒序） ----
    changes = []
    HERE_FILES = [
        ("hierarchy.json", "组织树元数据"),
        ("killed.json", "停用名单"),
        ("drafts.json", "草稿名单"),
        ("config_inherit_block.json", "config 单键 opt-out 名单"),
        ("skill_inherit_off.json", "skill 自动继承关闭名单"),
        ("constitution_state.json", "宪法订阅名单"),
    ]
    for fname, summary in HERE_FILES:
        p = HERE / fname
        if p.is_file():
            try:
                changes.append({"kind": "state", "target": fname,
                                "ts": p.stat().st_mtime, "summary": summary, "profile": None})
            except OSError:
                pass
    if CONSTITUTION_FILE.is_file():
        try:
            changes.append({"kind": "constitution", "target": "constitution.md",
                            "ts": CONSTITUTION_FILE.stat().st_mtime,
                            "summary": "宪法正文", "profile": None})
        except OSError:
            pass
    # 各 profile 的 .dashbak / .constibak（落位 / 宪法注入备份触发时写）
    for r in rows:
        nm = r["name"]
        pd = _profile_dir(nm)
        for fname, kind, summary in (
            ("config.yaml.dashbak", "backup", "落位/继承前 config 备份"),
            ("SOUL.md.constibak", "backup", "宪法注入前 SOUL 备份"),
        ):
            p = pd / fname
            if p.is_file():
                try:
                    changes.append({"kind": kind, "target": f"{nm}/{fname}",
                                    "ts": p.stat().st_mtime, "summary": summary, "profile": nm})
                except OSError:
                    pass
    changes.sort(key=lambda x: x["ts"], reverse=True)
    changes = changes[:20]
    now = time.time()
    recent_changes = []
    for c in changes:
        ago = max(0, int(now - c["ts"]))
        if ago < 60:
            ago_label = f"{ago} 秒前"
        elif ago < 3600:
            ago_label = f"{ago // 60} 分钟前"
        elif ago < 86400:
            ago_label = f"{ago // 3600} 小时前"
        else:
            ago_label = f"{ago // 86400} 天前"
        recent_changes.append({
            "kind": c["kind"], "target": c["target"], "summary": c["summary"],
            "profile": c["profile"],
            "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c["ts"])),
            "ago": ago_label, "ago_seconds": ago,
        })

    return {
        "health": {
            "agent_total": n_total,
            "main": n_main, "twin": n_twin, "sub": n_sub,
            "drafts": n_drafts,
            "killed": n_killed,
            "effective_killed": n_eff_killed,
            "lint_hard_total": total_hard,
            "lint_soft_total": total_soft,
            "default_mcp_count": len(default_mcp),
            "agents_with_mcp": agents_with_mcp,
            "const_subscribed": n_subbed,
        },
        "review": {
            "killed": sorted(ks),
            "hard_lint": hard_list,
            "drafts": sorted(drafts_set),
        },
        "recent_changes": recent_changes,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
    }


@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    """仪表盘聚合：系统健康 / 待你 Review / 最近变更。一次拿全（前端不用并发 N 个请求）。"""
    return _dashboard_summary()


# --------------------------------------------------------------------------- #
# 配置总览（只读）：把主 agent(default) 的 config.yaml 按 16 类归类 + 中文简介，
#   再附 .env 集成"是否已配置"（只看变量名在不在，绝不读取/返回值）。供仪表盘「配置」页。
#   安全：敏感命名键(*api_key/token/secret/password/credential/webhook…)的值一律打码；
#         .env 永远只报名字、不报值；本接口只读、不暴露任何凭据。
# --------------------------------------------------------------------------- #
_SENSITIVE_KEY_RE = re.compile(
    r"(api_key|secret|token|password|passwd|credential|webhook|cookie|bearer|client_secret|access_key|private_key)",
    re.I,
)


def _is_sensitive_key(k) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(k)))


def _fmt_cfg_val(key, v) -> str:
    """把单个配置值压成一行可读文本：敏感键打码、bool→开/关、空→—、容器→摘要。"""
    if _is_sensitive_key(key):
        return "••• 已设置" if v not in (None, "", [], {}) else "—"
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "开" if v else "关"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v if len(v) <= 90 else v[:88] + "…"
    if isinstance(v, list):
        if not v:
            return "—"
        if all(isinstance(x, (str, int, float, bool)) for x in v):
            s = "、".join(str(x) for x in v)
            return s if len(s) <= 90 else f"{len(v)} 项：{s[:80]}…"
        return f"{len(v)} 项"
    if isinstance(v, dict):
        if not v:
            return "—"
        ks = "、".join(str(k) for k in list(v.keys())[:6])
        return f"{len(v)} 项（{ks}{'…' if len(v) > 6 else ''}）"
    return str(v)


def _cfg_get(cfg: dict, path: str):
    cur = cfg
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _cfg_rows(cfg: dict, specs) -> list:
    """specs: [(dotted_path, 中文label)] → [{k,v}]（路径缺失时值显示 —）。"""
    return [{"k": label, "v": _fmt_cfg_val(path.split(".")[-1], _cfg_get(cfg, path))} for path, label in specs]


def _config_overview() -> dict:
    """主 agent 的 config.yaml 按类型归类 + .env 集成存在性（名字判断，不读值）。"""
    cfg = _read_config("default")
    env_names = set(_ROOT_ENV.keys())  # 只用名字判断集成/密钥是否配置；绝不读取值

    cats: list = []

    def cat(cid: str, label: str, desc: str, rows: list) -> None:
        cats.append({"id": cid, "label": label, "desc": desc, "items": rows})

    # 1 · 模型
    prov_keys = sorted({n[: -len("_API_KEY")].lower() for n in env_names if n.endswith("_API_KEY")})
    cat("model", "模型", "默认模型、供应商与失败回退链。", [
        *_cfg_rows(cfg, [
            ("model.default", "默认模型"),
            ("model.provider", "供应商"),
            ("model.base_url", "接口地址"),
            ("fallback_providers", "回退链"),
            ("model_catalog", "模型目录"),
        ]),
        {"k": "已配置密钥", "v": ("、".join(prov_keys) if prov_keys else "—")},
    ])

    # 2 · Agent 行为
    cat("agent", "Agent 行为", "对话回合、推理强度、工具使用与执行护栏。",
        _cfg_rows(cfg, [
            ("agent.max_turns", "最大轮数"),
            ("agent.reasoning_effort", "推理强度"),
            ("agent.tool_use_enforcement", "工具使用"),
            ("agent.image_input_mode", "图片输入"),
            ("agent.verbose", "详细日志"),
            ("tool_loop_guardrails", "循环护栏"),
            ("command_allowlist", "命令白名单"),
        ]))

    # 3 · 记忆
    cat("memory", "记忆", "长期记忆、用户画像、上下文压缩与提示缓存。",
        _cfg_rows(cfg, [
            ("memory.memory_enabled", "记忆开关"),
            ("memory.user_profile_enabled", "用户画像"),
            ("memory.memory_char_limit", "记忆上限(字)"),
            ("memory.nudge_interval", "提醒间隔"),
            ("context", "上下文"),
            ("compression", "压缩"),
            ("prompt_caching", "提示缓存"),
        ]))

    # 4 · 技能
    cat("skills", "技能", "技能加载目录、停用项与内联 Shell 策略。",
        _cfg_rows(cfg, [
            ("skills.external_dirs", "外部目录"),
            ("skills.disabled", "已停用"),
            ("skills.template_vars", "模板变量"),
            ("skills.inline_shell", "内联 Shell"),
            ("skills.guard_agent_created", "新建护栏"),
        ]))

    # 5 · MCP 服务
    mcp = cfg.get("mcp_servers") or {}
    mcp_rows = [
        {"k": nm, "v": ("已启用" if (s or {}).get("enabled", True) else "已停用") + " · " + str((s or {}).get("command", "") or "—")}
        for nm, s in mcp.items()
    ] or [{"k": "—", "v": "未配置任何 MCP 服务"}]
    cat("mcp_servers", "MCP 服务", "外部工具服务（Model Context Protocol）。", mcp_rows)

    # 6 · 工具集
    cat("toolsets", "工具集", "成套工具能力（CLI、平台等）与代码执行。",
        _cfg_rows(cfg, [
            ("toolsets", "已装工具集"),
            ("platform_toolsets", "平台工具集"),
            ("code_execution", "代码执行"),
            ("lsp", "语言服务"),
        ]))

    # 7 · 终端 · Web
    cat("terminal", "终端 · Web", "终端寿命、网页抓取、浏览器与读文件上限。",
        _cfg_rows(cfg, [
            ("terminal", "终端"),
            ("web", "网页"),
            ("browser", "浏览器"),
            ("file_read_max_chars", "读文件上限"),
            ("streaming", "流式输出"),
        ]))

    # 8 · 委派 · 编排
    cat("delegation", "委派 · 编排", "子 agent 并发派活与编排器。",
        _cfg_rows(cfg, [
            ("delegation.orchestrator_enabled", "编排器"),
            ("delegation.max_concurrent_children", "最大并发子"),
            ("delegation.max_spawn_depth", "最大派生深度"),
            ("delegation.child_timeout_seconds", "子超时(秒)"),
            ("delegation.subagent_auto_approve", "子自动批准"),
            ("network", "关系网"),
        ]))

    # 9 · 审批 · 安全
    cat("approvals", "审批 · 安全", "动作审批模式与安全 / 隐私护栏。",
        _cfg_rows(cfg, [
            ("approvals.mode", "审批模式"),
            ("approvals.cron_mode", "定时审批"),
            ("approvals.destructive_slash_confirm", "危险命令确认"),
            ("approvals.mcp_reload_confirm", "MCP 重载确认"),
            ("security", "安全"),
            ("privacy", "隐私"),
        ]))

    # 10 · 网关 · 集成（凭 .env 变量名是否存在判断，绝不读取 token 值）
    gw = [
        ("Discord", "DISCORD_BOT_TOKEN" in env_names),
        ("微信 WeChat", "WEIXIN_TOKEN" in env_names),
        ("Slack", "SLACK_BOT_TOKEN" in env_names or "SLACK_APP_TOKEN" in env_names),
        ("Telegram", "TELEGRAM_BOT_TOKEN" in env_names),
        ("WhatsApp", "WHATSAPP_TOKEN" in env_names or "WHATSAPP_ACCESS_TOKEN" in env_names),
        ("Matrix", "MATRIX_ACCESS_TOKEN" in env_names),
        ("Mattermost", "MATTERMOST_TOKEN" in env_names),
    ]
    cat("gateways", "网关 · 集成", "把 agent 接到 IM 平台的机器人通道（只看是否配置，不读密钥）。",
        [{"k": nm, "v": "已配置" if ok else "未配置"} for nm, ok in gw])

    # 11 · 语音 · 多模态
    cat("voice", "语音 · 多模态", "语音合成 / 识别与拟人延迟。",
        _cfg_rows(cfg, [
            ("tts", "语音合成"),
            ("stt", "语音识别"),
            ("voice", "语音"),
            ("human_delay", "拟人延迟"),
        ]))

    # 12 · 显示 · 界面
    pers = cfg.get("agent", {}).get("personalities") if isinstance(cfg.get("agent"), dict) else None
    cat("display", "显示 · 界面", "人格、语言、皮肤与仪表盘主题。", [
        *_cfg_rows(cfg, [
            ("display.personality", "当前人格"),
            ("display.language", "语言"),
            ("display.skin", "皮肤"),
            ("display.show_reasoning", "显示推理"),
            ("display.show_cost", "显示成本"),
            ("dashboard.theme", "仪表盘主题"),
        ]),
        {"k": "人格库", "v": (f"{len(pers)} 个" if isinstance(pers, dict) and pers else "—")},
    ])

    # 13 · 定时 · 任务
    cat("cron", "定时 · 任务", "定时触发、任务看板派活与会话管理。",
        _cfg_rows(cfg, [
            ("cron", "定时器"),
            ("kanban.dispatch_in_gateway", "看板派活"),
            ("kanban.auto_decompose", "自动拆解"),
            ("sessions", "会话"),
            ("session_reset", "会话重置"),
        ]))

    # 14 · 钩子 · 扩展
    cat("hooks", "钩子 · 扩展", "生命周期钩子与自动接受策略。",
        _cfg_rows(cfg, [
            ("hooks", "钩子"),
            ("hooks_auto_accept", "钩子自动接受"),
        ]))

    # 15 · 治理 · 维护
    cat("governance", "治理 · 维护", "记忆策展、辅助模型、更新与日志。",
        _cfg_rows(cfg, [
            ("curator.enabled", "策展开关"),
            ("curator.interval_hours", "策展间隔(时)"),
            ("auxiliary", "辅助模型"),
            ("updates", "更新"),
            ("logging", "日志"),
            ("timezone", "时区"),
        ]))

    # 16 · 分身 · Profiles
    names = _all_profile_names()
    labels = _load_labels()
    cat("profiles", "分身 · Profiles", "每个 profile 是一套隔离 agent（独立 HERMES_HOME）。", [
        {"k": "总数", "v": f"{len(names)} 个（含主 agent）"},
        {"k": "清单", "v": ("、".join(_label(n, labels) for n in names) if names else "—")},
    ])

    return {
        "source": str(HERMES_HOME / "config.yaml"),
        "config_version": cfg.get("_config_version"),
        "category_count": len(cats),
        "categories": cats,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
    }


@app.get("/api/config")
def api_config_overview():
    """主 agent 配置总览：按 16 类归类 + 中文简介。敏感值打码，绝不返回 .env 凭据值。"""
    return _config_overview()


# --------------------------------------------------------------------------- #
# 技能仓库（Warehouse）：离线"备用冷存储"，按来源(source)分类放"可能用得上但当下无场景"的技能。
#   关键：放在 ~/.hermes/.warehouse/ —— 在所有技能搜索路径之外 → 运行时绝不加载、不参与日常运行。
#   只能手动触发检索 / 晋升。晋升("装到 agent") = 把技能 MOVE 进目标 agent 的 skills/<source>/
#   下（中转区语义，不留母本）→ 它立刻变成普通物理技能；已"继承模式"的后代经 external_dirs 自动可见
#   （复用现有沿树继承机制，非新功能）。
# --------------------------------------------------------------------------- #
_WAREHOUSE_DIR = HERMES_HOME / ".warehouse"
_WH_SKIP = {".git", ".hub", ".github", "node_modules", "__pycache__", ".vaunt", "nix",
            "tests", "test", "docs", "plugins", "craft", "website", "scripts", "examples"}


def _warehouse_root() -> Path:
    _WAREHOUSE_DIR.mkdir(exist_ok=True)
    return _WAREHOUSE_DIR


def _wh_iter_skills(source_root: Path):
    """遍历一个来源目录，产出 (skill_dir, rel_from_source) —— 顶层含 SKILL.md 的目录即一个技能（不再下钻）。"""
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [x for x in dirnames if not x.startswith(".") and x not in _WH_SKIP]
        if "SKILL.md" in filenames:
            d = Path(dirpath)
            yield d, d.relative_to(source_root)
            dirnames[:] = []  # 技能目录内部不再深入


def _wh_skill_meta(skill_dir: Path, source: str, rel: Path) -> dict:
    desc = ""
    try:
        t = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="ignore")
        m = re.match(r"^---\s*\n(.*?)\n---", t, re.S)
        if m:                                         # 用真·YAML 解析 frontmatter（正确处理 `description: |` 块标量）
            fm = yaml.safe_load(m.group(1))
            if isinstance(fm, dict):
                desc = " ".join(str(fm.get("description") or "").split())[:160]
    except (OSError, yaml.YAMLError):
        pass
    parts = rel.parts
    group = parts[0] if len(parts) > 1 else "（根）"
    return {"type": "skill", "kind": "skill", "name": skill_dir.name, "rel": str(rel),
            "desc": desc, "group": group, "source": source, "config_key": None, "merge": None}


# 配置片段类条目：用 *.whcfg.yaml 文件描述，可被"插入"到 agent 的 config.yaml（合并进某个可继承键）。
#   格式：{kind, name, desc, config_key, merge: dict|list, value}。kind 仅用于展示分组。
#   实际主场是 MCP（按 agent 增减工具服务）；机制本身通用（任意 _INHERITABLE_KEYS 键），但 hooks/toolsets
#   等是全局性配置、不适合按 agent 进仓库，故不作为展示类型推介。
_WH_KIND_LABELS = {"mcp": "MCP 服务"}


def _wh_iter_configs(source_root: Path):
    """遍历来源目录，产出 (cfg_file, rel) —— 所有 *.whcfg.yaml 配置片段条目。"""
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [x for x in dirnames if not x.startswith(".") and x not in _WH_SKIP]
        for fn in filenames:
            if fn.endswith(".whcfg.yaml") or fn.endswith(".whcfg.yml"):
                p = Path(dirpath) / fn
                yield p, p.relative_to(source_root)


def _wh_config_meta(cfg_file: Path, source: str, rel: Path) -> dict:
    try:
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    kind = str(data.get("kind") or "config").lower()
    name = str(data.get("name") or cfg_file.name.replace(".whcfg.yaml", "").replace(".whcfg.yml", ""))
    desc = " ".join(str(data.get("desc") or "").split())[:160]
    return {"type": kind, "kind": kind, "name": name, "rel": str(rel), "desc": desc,
            "group": _WH_KIND_LABELS.get(kind, kind), "source": source,
            "config_key": data.get("config_key"), "merge": data.get("merge", "dict")}


def _warehouse_scan() -> dict:
    root = _warehouse_root()
    sources = []
    type_totals: dict = {}
    for sdir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        items = [_wh_skill_meta(sk, sdir.name, rel) for sk, rel in _wh_iter_skills(sdir)]
        items += [_wh_config_meta(cf, sdir.name, rel) for cf, rel in _wh_iter_configs(sdir)]
        items.sort(key=lambda x: (x["type"] != "skill", x["group"], x["name"].lower()))
        origin = ""
        gitcfg = sdir / ".git" / "config"
        if gitcfg.is_file():
            try:
                mm = re.search(r"url\s*=\s*(.+)", gitcfg.read_text(encoding="utf-8", errors="ignore"))
                if mm:
                    origin = mm.group(1).strip()
            except OSError:
                pass
        types: dict = {}
        for it in items:
            types[it["type"]] = types.get(it["type"], 0) + 1
            type_totals[it["type"]] = type_totals.get(it["type"], 0) + 1
        sources.append({"source": sdir.name, "origin": origin, "count": len(items),
                        "types": types, "groups": sorted({i["group"] for i in items}), "items": items})
    return {"root": str(root), "source_count": len(sources),
            "item_total": sum(s["count"] for s in sources), "type_totals": type_totals, "sources": sources}


@app.get("/api/warehouse")
def api_warehouse():
    """仓库总览：按来源列出所有冷存储技能（名/描述/分组/相对路径）。供仪表盘卡片浏览。"""
    return _warehouse_scan()


@app.get("/api/warehouse/search")
def api_warehouse_search(q: str):
    """检索仓库（名/描述模糊匹配）——给"工作中遇到难题→让 agent 搜仓库找能用的技能"那个场景用。"""
    ql = (q or "").strip().lower()
    if not ql:
        return {"q": q, "results": [], "total": 0}
    out = []
    for s in _warehouse_scan()["sources"]:
        for it in s["items"]:
            if ql in it["name"].lower() or ql in it["desc"].lower():
                out.append(it)
    return {"q": q, "total": len(out), "results": out[:200]}


class WHInstallReq(BaseModel):
    source: str
    rel: str
    target: str


@app.post("/api/warehouse/install")
def api_warehouse_install(req: WHInstallReq):
    """晋升：把仓库里的一个技能 MOVE 进目标 agent 的 skills/<source>/<name>（移出仓库，不留母本）。
       装完即普通物理技能；目标的"继承模式"后代经 external_dirs 自动继承（复用现有机制，无需重连）。"""
    target = (req.target or "").strip()
    if target != "default" and not _PROFILE_ID_RE.match(target):
        raise HTTPException(400, "非法目标 agent")
    if not _profile_dir(target).is_dir():
        raise HTTPException(404, "目标 agent 不存在")
    src = (req.source or "").strip()
    if not src or "/" in src or ".." in src or src.startswith("."):
        raise HTTPException(400, "非法来源名")
    root = _warehouse_root().resolve()
    source_root = (root / src).resolve()
    try:
        source_root.relative_to(root)
    except ValueError:
        raise HTTPException(400, "来源越界")
    if not source_root.is_dir():
        raise HTTPException(404, "来源不存在")
    rel = (req.rel or "").strip().strip("/")
    if not rel or any(seg in ("", ".", "..") for seg in rel.split("/")):
        raise HTTPException(400, "非法条目路径")
    item_path = (source_root / rel).resolve()
    try:
        item_path.relative_to(source_root)
    except ValueError:
        raise HTTPException(400, "条目路径越界")

    # —— 技能（目录类）：MOVE 进 agent 的 skills/<source>/ —— #
    if item_path.is_dir() and (item_path / "SKILL.md").is_file():
        dest = _profile_dir(target) / "skills" / src / item_path.name
        if dest.exists():
            raise HTTPException(409, f"目标已存在同名技能：{src}/{item_path.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item_path), str(dest))
        return {"ok": True, "type": "skill", "target": target, "installed": f"{src}/{item_path.name}",
                "dest": str(dest.relative_to(HERMES_HOME))}

    # —— 配置片段（MCP / 工具集 / 钩子…）：合并进 agent 的 config.yaml，然后移出仓库 —— #
    if item_path.is_file() and item_path.name.endswith((".whcfg.yaml", ".whcfg.yml")):
        try:
            data = yaml.safe_load(item_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise HTTPException(400, f"配置片段解析失败：{e}")
        kind = str(data.get("kind") or "config")
        name = str(data.get("name") or item_path.name.replace(".whcfg.yaml", "").replace(".whcfg.yml", ""))
        config_key = data.get("config_key")
        merge = data.get("merge", "dict")
        value = data.get("value")
        if not config_key or not isinstance(config_key, str) or config_key not in _INHERITABLE_KEYS:
            raise HTTPException(400, f"config_key 非法或不可继承：{config_key!r}")
        if value is None:
            raise HTTPException(400, "配置片段缺少 value")
        cfg = _read_config(target)
        if config_key not in cfg:                      # 不自有该键但从父级继承 → 先落进继承值，避免"自有覆盖"抹掉已继承项
            eff = _effective_config(target)
            if config_key in eff:
                cfg[config_key] = copy.deepcopy(eff[config_key])
        if merge == "list":
            lst = cfg.get(config_key) or []
            if not isinstance(lst, list):
                raise HTTPException(409, f"{config_key} 当前不是列表，无法追加")
            if value in lst:
                raise HTTPException(409, f"{config_key} 已包含该项")
            cfg[config_key] = lst + [value]
        else:  # dict（mcp_servers / hooks…）
            d = cfg.get(config_key) or {}
            if not isinstance(d, dict):
                raise HTTPException(409, f"{config_key} 当前不是字典，无法插入")
            if name in d:
                raise HTTPException(409, f"{config_key} 已存在 {name}")
            d[name] = value
            cfg[config_key] = d
        # 写前备份（safe_dump 会重排/去注释；备份让其可逆）
        cfgp = _profile_dir(target) / "config.yaml"
        if cfgp.is_file():
            try:
                (_profile_dir(target) / "config.yaml.whbak").write_text(cfgp.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        _write_config(target, cfg)
        item_path.unlink()  # 移出仓库（中转区，不留母本）
        return {"ok": True, "type": kind, "target": target,
                "installed": f"{config_key}.{name}", "dest": f"{target}/config.yaml",
                "note": "已合并进 config.yaml（已备份 .whbak）；继承模式后代经 config 继承自动获得"}

    raise HTTPException(404, "未知仓库条目（既非技能目录、也非 *.whcfg.yaml 配置片段）")


# --------------------------------------------------------------------------- #
# 分配（无 master / 直接复制模型）：把一个配置从它当前所在处「复制」一份到多个目标 agent。
#   - 技能 → copytree 进 target/skills/<source>/<name>（独立副本）
#   - 配置片段 → 合并进 target/config.yaml（同 install 逻辑，先 seed 继承值再并、写前备份）
#   复制时给每个目标记一笔【内容指纹 + 血缘】到 distribution.json —— 为后续"漂移检测/选择性
#   重新分配"铺路（同血缘副本指纹不一致 = 漂移）。复制不删源（源是它当前的家）；可选 remove_from_warehouse。
# --------------------------------------------------------------------------- #
_DISTRIBUTION_FILE = HERE / "distribution.json"


def _fingerprint_dir(d: Path) -> str:
    """技能目录内容指纹：按相对路径排序，喂 (相对路径 + 文件字节)。跳过 .git。"""
    h = hashlib.sha256()
    try:
        files = sorted((p for p in d.rglob("*") if p.is_file() and "/.git/" not in str(p)),
                       key=lambda x: str(x.relative_to(d)))
    except OSError:
        return ""
    for p in files:
        h.update(str(p.relative_to(d)).encode("utf-8"))
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:16]


def _fingerprint_value(v) -> str:
    """配置片段值的指纹（规范化 JSON）。"""
    return hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _load_distribution() -> dict:
    try:
        return json.loads(_DISTRIBUTION_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _save_distribution(d: dict) -> None:
    try:
        tmp = _DISTRIBUTION_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_DISTRIBUTION_FILE)
    except OSError:
        pass


def _record_distribution(kind: str, name: str, agent: str, fp: str, origin: str, loc: dict) -> None:
    """登记一份副本：key = '<kind>:<name>'，记 该 agent 的内容指纹 + 血缘 + 定位(loc)。
       loc 用于日后重算当前指纹/对齐：技能 {type:skill, path:'skills/..'}；MCP {type:mcp, config_key:'mcp_servers'}。"""
    d = _load_distribution()
    d.setdefault(f"{kind}:{name}", {})[agent] = {"fp": fp, "origin": origin, "ts": time.time(), "loc": loc}
    _save_distribution(d)


def _current_fingerprint(name: str, agent: str, rec: dict):
    """重算某 agent 当前这份副本的内容指纹（不在了返回 None）。"""
    loc = rec.get("loc") or {}
    if loc.get("type") == "skill":
        p = _profile_dir(agent) / loc.get("path", "")
        return _fingerprint_dir(p) if (p / "SKILL.md").is_file() else None
    if loc.get("type") == "mcp":
        v = (_read_config(agent).get(loc.get("config_key")) or {}).get(name)
        return _fingerprint_value(v) if v is not None else None
    return None


class AllocateReq(BaseModel):
    source: str                      # 仓库来源名
    rel: str                         # 条目在来源内的相对路径（技能目录 / *.whcfg.yaml）
    targets: list[str]               # 复制到这些 agent
    remove_from_warehouse: bool = False  # 全部成功后是否把仓库里那份删掉（默认保留=冷存档）


@app.post("/api/warehouse/allocate")
def api_warehouse_allocate(req: AllocateReq):
    """把仓库里的一个配置「复制」到多个目标 agent（无 master 模型）；每份记内容指纹+血缘。"""
    src = (req.source or "").strip()
    if not src or "/" in src or ".." in src or src.startswith("."):
        raise HTTPException(400, "非法来源名")
    root = _warehouse_root().resolve()
    source_root = (root / src).resolve()
    try:
        source_root.relative_to(root)
    except ValueError:
        raise HTTPException(400, "来源越界")
    rel = (req.rel or "").strip().strip("/")
    if not rel or any(seg in ("", ".", "..") for seg in rel.split("/")):
        raise HTTPException(400, "非法条目路径")
    item_path = (source_root / rel).resolve()
    try:
        item_path.relative_to(source_root)
    except ValueError:
        raise HTTPException(400, "条目路径越界")
    targets = [t.strip() for t in (req.targets or []) if t and str(t).strip()]
    if not targets:
        raise HTTPException(400, "未选择目标 agent")
    for t in targets:
        if t != "default" and not _PROFILE_ID_RE.match(t):
            raise HTTPException(400, f"非法目标 agent: {t}")
        if not _profile_dir(t).is_dir():
            raise HTTPException(404, f"目标 agent 不存在: {t}")
    origin = f"warehouse:{src}/{rel}"
    results = []

    is_skill = item_path.is_dir() and (item_path / "SKILL.md").is_file()
    is_cfg = item_path.is_file() and item_path.name.endswith((".whcfg.yaml", ".whcfg.yml"))
    if not (is_skill or is_cfg):
        raise HTTPException(404, "未知仓库条目")

    if is_cfg:
        try:
            data = yaml.safe_load(item_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise HTTPException(400, f"配置片段解析失败：{e}")
        kind = str(data.get("kind") or "config")
        cname = str(data.get("name") or item_path.name.replace(".whcfg.yaml", "").replace(".whcfg.yml", ""))
        config_key = data.get("config_key")
        merge = data.get("merge", "dict")
        value = data.get("value")
        if not config_key or not isinstance(config_key, str) or config_key not in _INHERITABLE_KEYS:
            raise HTTPException(400, f"config_key 非法或不可继承：{config_key!r}")
        if value is None:
            raise HTTPException(400, "配置片段缺少 value")
        fp = _fingerprint_value(value)
        for t in targets:
            cfg = _read_config(t)
            if config_key not in cfg:                      # 先落继承值，避免抹掉已继承项
                eff = _effective_config(t)
                if config_key in eff:
                    cfg[config_key] = copy.deepcopy(eff[config_key])
            if merge == "list":
                lst = cfg.get(config_key) or []
                if not isinstance(lst, list):
                    results.append({"target": t, "ok": False, "msg": f"{config_key} 不是列表"}); continue
                if value in lst:
                    results.append({"target": t, "ok": False, "msg": "已存在"}); continue
                cfg[config_key] = lst + [value]
            else:
                dct = cfg.get(config_key) or {}
                if not isinstance(dct, dict):
                    results.append({"target": t, "ok": False, "msg": f"{config_key} 不是字典"}); continue
                if cname in dct:
                    results.append({"target": t, "ok": False, "msg": "已存在"}); continue
                dct[cname] = value; cfg[config_key] = dct
            cfgp = _profile_dir(t) / "config.yaml"
            if cfgp.is_file():
                try:
                    (_profile_dir(t) / "config.yaml.whbak").write_text(cfgp.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    pass
            _write_config(t, cfg)
            _record_distribution(kind, cname, t, fp, origin, {"type": "mcp", "config_key": config_key})
            results.append({"target": t, "ok": True, "dest": f"{t}/config.yaml"})
    else:  # 技能：复制目录
        name = item_path.name
        kind = "skill"
        for t in targets:
            dest = _profile_dir(t) / "skills" / src / name
            if dest.exists():
                results.append({"target": t, "ok": False, "msg": "目标已有同名技能"}); continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copytree(str(item_path), str(dest), ignore=shutil.ignore_patterns(".git"))
            except OSError as e:
                results.append({"target": t, "ok": False, "msg": str(e)[:120]}); continue
            _record_distribution(kind, name, t, _fingerprint_dir(dest), origin,
                                 {"type": "skill", "path": str(dest.relative_to(_profile_dir(t)))})
            results.append({"target": t, "ok": True, "dest": str(dest.relative_to(HERMES_HOME))})

    ok_n = sum(1 for r in results if r["ok"])
    removed = False
    if req.remove_from_warehouse and ok_n == len(targets):
        try:
            if is_cfg:
                item_path.unlink()
            else:
                shutil.rmtree(item_path)
            removed = True
        except OSError:
            pass
    return {"ok": ok_n > 0, "copied": ok_n, "total": len(targets),
            "removed_from_warehouse": removed, "results": results}


@app.get("/api/distribution")
def api_distribution():
    """分配总览 + 漂移检测：按"同血缘"分组（一个配置被分到哪些 agent），重算各副本当前指纹，
       同组指纹不一致 = 漂移（某副本自进化了）。每份标 changed(对比分配时基线) / present(还在不在)。"""
    dist = _load_distribution()
    groups = []
    for key, agents in dist.items():
        kind, _, name = key.partition(":")
        copies = []
        for agent, rec in agents.items():
            cur = _current_fingerprint(name, agent, rec)
            copies.append({
                "agent": agent, "label": _label(agent),
                "baseline_fp": rec.get("fp"), "current_fp": cur,
                "present": cur is not None,
                "changed": cur is not None and cur != rec.get("fp"),
                "ts": rec.get("ts"),
            })
        copies.sort(key=lambda c: (not c["changed"], c["agent"]))
        present_fps = {c["current_fp"] for c in copies if c["present"]}
        drifted = len(present_fps) > 1
        groups.append({
            "kind": kind, "name": name, "n_copies": len(copies),
            "shared": len(copies) > 1, "drifted": drifted,
            "n_changed": sum(1 for c in copies if c["changed"]),
            "copies": copies,
        })
    groups.sort(key=lambda g: (not g["drifted"], g["kind"], g["name"]))
    return {"groups": groups, "total": len(groups),
            "drifted_count": sum(1 for g in groups if g["drifted"])}


class ReconcileReq(BaseModel):
    kind: str
    name: str
    source: str               # 用哪个 agent 的版本当源
    targets: list[str]        # 覆盖到这些 agent（须是已持有该配置的 agent）


@app.post("/api/reconcile")
def api_reconcile(req: ReconcileReq):
    """选择性重新分配：用 source agent 的版本，覆盖到选中的 target agent（只在已持有该配置者中选）。
       不强推——你挑源版本、挑要对齐的人。技能=覆盖目录；MCP=覆盖 config 里那一项。"""
    key = f"{req.kind}:{req.name}"
    group = _load_distribution().get(key, {})
    if req.source not in group:
        raise HTTPException(404, f"源 agent 未持有该配置：{req.source}")
    src_rec = group[req.source]
    src_loc = src_rec.get("loc") or {}
    targets = [t.strip() for t in (req.targets or []) if t and str(t).strip() and t != req.source]
    if not targets:
        raise HTTPException(400, "未选择要对齐的目标 agent")
    for t in targets:
        if t not in group:
            raise HTTPException(400, f"目标未持有该配置（只能对齐已持有者）：{t}")
    results = []
    if src_loc.get("type") == "skill":
        sp = _profile_dir(req.source) / src_loc.get("path", "")
        if not (sp / "SKILL.md").is_file():
            raise HTTPException(404, "源副本已不存在")
        for t in targets:
            tp = _profile_dir(t) / (group[t].get("loc") or {}).get("path", "")
            try:
                if tp.exists():
                    shutil.rmtree(tp)
                tp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(str(sp), str(tp), ignore=shutil.ignore_patterns(".git"))
            except OSError as e:
                results.append({"target": t, "ok": False, "msg": str(e)[:120]}); continue
            _record_distribution(req.kind, req.name, t, _fingerprint_dir(tp), f"reconcile:{req.source}",
                                 {"type": "skill", "path": str(tp.relative_to(_profile_dir(t)))})
            results.append({"target": t, "ok": True})
    elif src_loc.get("type") == "mcp":
        ck = src_loc.get("config_key")
        val = (_read_config(req.source).get(ck) or {}).get(req.name)
        if val is None:
            raise HTTPException(404, "源配置已不存在")
        fp = _fingerprint_value(val)
        for t in targets:
            cfg = _read_config(t)
            d = cfg.get(ck) or {}
            if not isinstance(d, dict):
                results.append({"target": t, "ok": False, "msg": f"{ck} 非字典"}); continue
            d[req.name] = copy.deepcopy(val); cfg[ck] = d
            cfgp = _profile_dir(t) / "config.yaml"
            if cfgp.is_file():
                try:
                    (_profile_dir(t) / "config.yaml.whbak").write_text(cfgp.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    pass
            _write_config(t, cfg)
            _record_distribution(req.kind, req.name, t, fp, f"reconcile:{req.source}", {"type": "mcp", "config_key": ck})
            results.append({"target": t, "ok": True})
    else:
        raise HTTPException(400, "未知配置类型")
    ok_n = sum(1 for r in results if r["ok"])
    return {"ok": ok_n > 0, "aligned": ok_n, "total": len(targets), "results": results}


# --------------------------------------------------------------------------- #
# Vault（全局知识库）：单一全局库，指向用户的 llmwiki / Obsidian 库（默认 ~/.hermes/vault）。
#   所有 agent 共享同一个库——检索 / RAG 交给 llmwiki（语义 + wiki 结构），仪表盘只做
#   浏览 / 读 / 写这层薄壳，不重复造索引。路径在 vault_config.json 配置，可随时改。
#   设计取舍：不分 per-agent、不做继承、不在仪表盘删笔记（删走 Obsidian / 管道，避免误删真实知识）。
# --------------------------------------------------------------------------- #
_VAULT_CONFIG_FILE = HERE / "vault_config.json"
_VAULT_SKIP_DIRS = {".obsidian", ".trash", ".git", ".idea", "__pycache__", "node_modules"}


def _vault_root() -> Path:
    """全局知识库根目录。优先 vault_config.json 的 root，否则 ~/.hermes/vault。每次读以支持热改。"""
    try:
        cfg = json.loads(_VAULT_CONFIG_FILE.read_text(encoding="utf-8"))
        p = (cfg.get("root") or "").strip()
        if p:
            return Path(os.path.expanduser(p))
    except (OSError, json.JSONDecodeError):
        pass
    return Path(os.path.expanduser("~/.hermes/vault"))


def _safe_vault_path(rel: str) -> str:
    """规范化笔记相对路径：必须 .md 结尾、不含 .. / 不能绝对路径 / 不能动备份。返回净化后的相对路径。"""
    rel = (rel or "").strip().lstrip("/")
    if not rel or "//" in rel:
        raise HTTPException(400, f"非法笔记路径: {rel!r}")
    segs = rel.split("/")
    if any(seg in ("", ".", "..") for seg in segs):
        raise HTTPException(400, "路径段非法")
    if any(seg.startswith(".") for seg in segs):     # 拒绝 .obsidian/.trash 等隐藏段（与树视图一致）
        raise HTTPException(400, "不能写入隐藏目录 / 文件")
    if rel.lower().endswith(".dashbak"):
        raise HTTPException(400, "不能直接操作备份文件")
    if not rel.lower().endswith(".md"):
        raise HTTPException(400, "笔记必须 .md 结尾")
    if len(rel) > 240:
        raise HTTPException(400, "路径过长")
    return rel


def _vault_resolve(rel: str) -> Path:
    """相对路径 → vault 内绝对路径，并确保 resolve 后仍在 vault 根内（防 symlink / .. 逃逸）。"""
    rel = _safe_vault_path(rel)
    root = _vault_root().resolve()
    full = (root / rel).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise HTTPException(400, "路径越界")
    return full


def _vault_tree(p: Path, root: Path, depth: int = 0) -> list:
    """递归构建 p 下的目录树。跳过隐藏目录 / 备份文件；只收 .md。返回 node 列表（dir 先于 file）。"""
    nodes = []
    try:
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    except OSError:
        return nodes
    for c in entries:
        name = c.name
        if c.is_dir():
            if name in _VAULT_SKIP_DIRS or name.startswith(".") or depth > 12:
                continue
            nodes.append({"type": "dir", "name": name,
                          "path": str(c.relative_to(root)),
                          "children": _vault_tree(c, root, depth + 1)})
        elif c.is_file():
            low = name.lower()
            if not low.endswith(".md") or low.endswith(".dashbak") or name.startswith("."):
                continue
            try:
                st = c.stat()
            except OSError:
                continue
            nodes.append({"type": "file", "name": name, "title": name[:-3],
                          "path": str(c.relative_to(root)),
                          "size": st.st_size, "mtime": st.st_mtime})
    return nodes


def _vault_count_files(nodes: list) -> int:
    n = 0
    for x in nodes:
        if x.get("type") == "file":
            n += 1
        else:
            n += _vault_count_files(x.get("children") or [])
    return n


@app.get("/api/vault")
def api_vault_tree():
    """全局知识库的文件夹树（跳过 .obsidian/.trash 等隐藏目录与备份；只列 .md）。"""
    root = _vault_root()
    if not root.is_dir():
        return {"root": str(root), "exists": False, "tree": [], "file_count": 0}
    rroot = root.resolve()                      # 用同一个 resolve 后的根遍历 + 算相对路径（防 /var→/private/var 这类符号链接）
    tree = _vault_tree(rroot, rroot)
    return {"root": str(root), "exists": True, "tree": tree,
            "file_count": _vault_count_files(tree)}


@app.get("/api/vault/note")
def api_vault_note_get(path: str):
    """读一篇笔记的完整 markdown。"""
    full = _vault_resolve(path)
    if not full.is_file():
        raise HTTPException(404, "笔记不存在")
    try:
        content = full.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"读取失败: {e}")
    st = full.stat()
    return {
        "path": _safe_vault_path(path),
        "abs_path": str(full),
        "content": content,
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


class VaultWriteReq(BaseModel):
    path: str
    content: str


@app.post("/api/vault/note")
def api_vault_note_write(req: VaultWriteReq):
    """写一篇笔记到全局知识库（覆盖前自动 .dashbak 备份）。新建会自动建中间目录。"""
    if not _vault_root().is_dir():
        raise HTTPException(404, f"知识库目录不存在：{_vault_root()}")
    full = _vault_resolve(req.path)
    full.parent.mkdir(parents=True, exist_ok=True)
    overwritten = False
    if full.is_file():
        overwritten = True
        try:
            bak = full.with_suffix(full.suffix + ".dashbak")
            bak.write_text(full.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass  # 备份失败不阻塞写入
    try:
        full.write_text(req.content or "", encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"写入失败: {e}")
    return {"ok": True, "path": _safe_vault_path(req.path),
            "size": full.stat().st_size, "overwritten": overwritten}


# --------------------------------------------------------------------------- #
# Constitution（宪法）· 分层治理：每个节点(agent)写自己那层 <profile>/constitution.md；某 agent 的
#   "有效宪法" = 它整条祖先链(根→…→自身)各层叠加（按"组织级X / 簇 / 本级"分块），注入到该 agent
#   SOUL.md 顶部受管区块（区块下方=它自己的人格，绝不动）。改某节点的层 → 自动重刷它整棵子树。
#   default(X) 的层 = ~/.hermes/constitution.md。分身(twin) 的 SOUL symlink 共享 X，随 X 自动、不单独注入。
#   这是提示词级强引导（让模型自觉守红线），非机械拦截——真要拦工具属 hooks/权限层。
# --------------------------------------------------------------------------- #
CONSTITUTION_FILE = HERMES_HOME / "constitution.md"   # = default(X) 的那一层
_CONST_BEGIN = "<!-- ⚖ HERMES CONSTITUTION · 由仪表盘统一管理（沿组织树分层继承），勿手改本区块（下方是本 agent 的人格，可自由编辑） -->"
_CONST_END = "<!-- ⚖ END CONSTITUTION -->"
_CONST_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_CONST_RULE_HEADING_RE = re.compile(
    r"^(#{1,6})\s+\[(rule|override|disable):([A-Za-z0-9][A-Za-z0-9_.-]{0,95})\]\s*(.*)$",
    re.I,
)


def _node_layer(name: str) -> str:
    """该节点自己贡献的宪法层（<profile>/constitution.md）。"""
    p = _profile_dir(name) / "constitution.md"
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except OSError:
        return ""


def _save_node_layer(name: str, text: str) -> None:
    try:
        (_profile_dir(name) / "constitution.md").write_text(text or "", encoding="utf-8")
    except OSError:
        pass


def _constitution_chain(name: str) -> list:
    """根(default)→name 的节点链，用于按祖先顺序叠加宪法层。"""
    chain, cur, seen, guard = [], name, set(), 0
    while cur and cur not in seen and guard < 30:
        seen.add(cur); chain.append(cur); guard += 1
        if cur == MAIN_AGENT:
            break
        cur = effective_parent(cur)
    chain.reverse()
    return chain


def _constitution_layers(name: str) -> list:
    """name 的有效宪法分层明细 [{node,label,text,is_self}]（只含有内容的层，根在前）。"""
    labels = _load_labels()
    out = []
    for node in _constitution_chain(name):
        layer = _node_layer(node).strip()
        if layer:
            lbl = f"组织级 · {_label(node, labels)}" if node == MAIN_AGENT else _label(node, labels)
            out.append({"node": node, "label": lbl, "text": layer, "is_self": node == name})
    return out


def _parse_constitution_entries(node: str, label: str, text: str, is_self: bool) -> list:
    """把一层 constitution.md 解析成 legacy 文本块 + 显式规则块。

    规则块语法（Markdown heading）：
      ### [rule:family-love] 家庭偏好
      ### [override:family-love] 家庭偏好
      ### [disable:family-love]

    未标记文本保持旧行为：作为该层的自由文本追加，不能被子级按条覆盖。
    """
    entries, loose, current = [], [], None

    def flush_loose() -> None:
        nonlocal loose
        chunk = "\n".join(loose).strip()
        if chunk:
            entries.append({"kind": "text", "node": node, "label": label, "text": chunk, "is_self": is_self})
        loose = []

    def flush_rule() -> None:
        nonlocal current
        if current:
            current["body"] = "\n".join(current.pop("body_lines", [])).strip()
            entries.append(current)
            current = None

    for line in (text or "").splitlines():
        match = _CONST_RULE_HEADING_RE.match(line)
        if match:
            flush_rule()
            flush_loose()
            op = match.group(2).lower()
            rule_id = match.group(3).lower()
            title = match.group(4).strip()
            current = {
                "kind": "rule",
                "op": op,
                "rule_id": rule_id,
                "title": title,
                "node": node,
                "label": label,
                "is_self": is_self,
                "body_lines": [],
            }
        elif current:
            current["body_lines"].append(line)
        else:
            loose.append(line)
    flush_rule()
    flush_loose()
    return entries


def _render_constitution_rule(entry: dict) -> str:
    title = (entry.get("title") or "").strip()
    body = (entry.get("body") or "").strip()
    parts = []
    if title:
        parts.append(f"### {title}")
    if body:
        parts.append(body)
    return "\n".join(parts).strip()


def _resolved_constitution(name: str) -> dict:
    """沿祖先链按 rule_id 解析宪法。

    - text(未标记): 旧式追加块，永远参与最终宪法。
    - rule / override: 写入同一 rule_id 的当前值；子级同 id 会遮住父级同 id。
    - disable: 屏蔽同一 rule_id 的祖先规则；后代可再次 rule/override 打开。

    判断依据是显式 rule_id/操作痕迹，不做语义猜测，也不比较文本是否相同。
    """
    labels = _load_labels()
    items: list[dict] = []
    rules: dict[str, dict] = {}
    rule_seen: set[str] = set()
    resolved_rules: list[dict] = []

    for node in _constitution_chain(name):
        layer = _node_layer(node).strip()
        if not layer:
            continue
        label = f"组织级 · {_label(node, labels)}" if node == MAIN_AGENT else _label(node, labels)
        for entry in _parse_constitution_entries(node, label, layer, node == name):
            if entry["kind"] == "text":
                items.append({"kind": "text", "entry": entry})
                continue

            rule_id = entry["rule_id"]
            if rule_id not in rule_seen:
                rule_seen.add(rule_id)
                items.append({"kind": "rule", "rule_id": rule_id})

            if entry["op"] == "disable":
                rules[rule_id] = {**entry, "disabled": True}
            else:
                rules[rule_id] = {**entry, "disabled": False}

    sections = []
    for item in items:
        if item["kind"] == "text":
            entry = item["entry"]
            sections.append({
                "kind": "text",
                "node": entry["node"],
                "label": entry["label"],
                "text": entry["text"],
                "is_self": entry["is_self"],
            })
            continue

        rule_id = item["rule_id"]
        entry = rules.get(rule_id)
        if not entry:
            continue
        resolved_rules.append({
            "id": rule_id,
            "op": entry.get("op"),
            "node": entry.get("node"),
            "label": entry.get("label"),
            "title": entry.get("title") or "",
            "disabled": bool(entry.get("disabled")),
            "is_self": bool(entry.get("is_self")),
        })
        if entry.get("disabled"):
            continue
        text = _render_constitution_rule(entry)
        if not text:
            continue
        suffix = " · override" if entry.get("op") == "override" else ""
        sections.append({
            "kind": "rule",
            "rule_id": rule_id,
            "op": entry.get("op"),
            "node": entry["node"],
            "label": f"{entry['label']} · {rule_id}{suffix}",
            "text": text,
            "is_self": entry["is_self"],
        })

    effective = "\n\n".join(f"## ⟦{s['label']}⟧\n{s['text']}" for s in sections if (s.get("text") or "").strip())
    return {"sections": sections, "rules": resolved_rules, "effective": effective}


def _effective_constitution(name: str) -> str:
    """name 的有效宪法正文（祖先链叠加 + rule_id 覆盖/屏蔽解析后，带分块标题）。"""
    return _resolved_constitution(name)["effective"]


def _constitution_governed(name: str) -> bool:
    """该 agent 当前 SOUL 是否"真注入了"宪法块（实际状态，非"应该"）。分身看 X 的 SOUL。
       注意：定义在 _CONST_BEGIN 之后引用——本函数运行时才读，模块加载顺序无碍。"""
    tgt = MAIN_AGENT if is_main_twin(name) else name
    p = _profile_dir(tgt) / "SOUL.md"
    try:
        return p.is_file() and _CONST_BEGIN in p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _load_const_subs() -> set:
    """（兼容名）被宪法治理的 agent 集合 = 有效宪法非空者。供仪表盘/lint 复用。"""
    return {n for n in _all_profile_names() if _constitution_governed(n)}


def _strip_constitution(soul: str) -> str:
    """去掉 SOUL.md 里的受管宪法区块（含结束标记后紧跟的空行）；没有则原样返回。"""
    b = soul.find(_CONST_BEGIN)
    if b == -1:
        return soul
    e = soul.find(_CONST_END, b)
    if e == -1:
        return soul  # 结束标记缺失（被手改坏）——保守不动
    rest = soul[e + len(_CONST_END):].lstrip("\n")
    return soul[:b] + rest


def _inject_constitution(soul: str, const: str) -> str:
    """把宪法正文作为受管区块放到 SOUL.md 顶部；先剥旧块，保证幂等。为空=只剥不加。"""
    base = _strip_constitution(soul)
    const = (const or "").strip()
    if not const:
        return base
    return f"{_CONST_BEGIN}\n{const}\n{_CONST_END}\n\n" + base.lstrip("\n")


def _inject_soul(name: str) -> bool:
    """把 name 的有效宪法注入它 SOUL.md（首次改前备份 .constibak）。返回是否被治理。
    分身(architect/steward)有独立的 SOUL.md（真文件、非 symlink，含各自"分身模式"角色行），
    但它们【就是】主 agent，所以注入主 agent(X) 的有效宪法（而非走自己的链）——
    否则分身会成为全树唯一拿不到宪法的节点（它们恰恰是最高权限的 X 本体）。"""
    soul_path = _profile_dir(name) / "SOUL.md"
    eff = _effective_constitution(MAIN_AGENT if is_main_twin(name) else name)
    try:
        soul = soul_path.read_text(encoding="utf-8") if soul_path.is_file() else ""
    except OSError:
        soul = ""
    bak = _profile_dir(name) / "SOUL.md.constibak"
    if soul_path.is_file() and not bak.is_file() and _CONST_BEGIN not in soul:
        try:
            bak.write_text(soul, encoding="utf-8")  # 备份"加宪法前"的原始人格
        except OSError:
            pass
    try:
        soul_path.write_text(_inject_constitution(soul, eff), encoding="utf-8")
    except OSError:
        pass
    return bool(eff.strip())


def _materialize_constitution(node: str) -> int:
    """重刷 node 及其所有后代的 SOUL 宪法块（祖先链变动影响的范围）。返回处理的 agent 数。"""
    nodes = build_tree()["nodes"]
    affected, stack, seen = [], [node], set()
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n); affected.append(n)
        stack.extend(nodes.get(n, {}).get("children", []))
    for a in affected:
        _inject_soul(a)
    return len(affected)


_last_const_sig = None


def _constitution_layer_path(name: str) -> Path:
    return CONSTITUTION_FILE if name == MAIN_AGENT else _profile_dir(name) / "constitution.md"


def _constitution_files_sig() -> dict:
    """所有宪法层文件的 mtime/size 指纹。缺失也入表,用于侦测删除后剥离旧注入。"""
    names = set(_all_profile_names())
    names.add(MAIN_AGENT)
    sig = {}
    for name in names:
        p = _constitution_layer_path(name)
        try:
            st = p.stat()
            sig[name] = (str(p), st.st_mtime_ns, st.st_size) if p.is_file() else (str(p), None, None)
        except OSError:
            sig[name] = (str(p), None, None)
    return sig


def _constitution_sync_tick(force: bool = False) -> None:
    """后台 tick:侦测任意 constitution.md 直接改动,自动物化到该节点及其子树 SOUL。
       这补齐"dashboard UI 保存会 resync,但 agent/编辑器直接写文件不会 resync"的缺口。
       根 X 变化会重刷全树；子节点变化只重刷自己的子树。"""
    global _last_const_sig
    with _SYNC_LOCK:
        sig = _constitution_files_sig()
        if force or _last_const_sig is None:
            _materialize_constitution(MAIN_AGENT)
            _last_const_sig = _constitution_files_sig()
            return
        if sig == _last_const_sig:
            return

        changed = sorted(name for name in set(sig) | set(_last_const_sig) if sig.get(name) != _last_const_sig.get(name))
        if MAIN_AGENT in changed:
            _materialize_constitution(MAIN_AGENT)
        else:
            for name in changed:
                if _profile_dir(name).is_dir():
                    _materialize_constitution(name)
        _last_const_sig = _constitution_files_sig()


class ConstLayerReq(BaseModel):
    text: str


@app.get("/api/constitution")
def api_constitution_overview():
    """宪法总览：各节点"本层"是否有内容 + 每个 agent 是否被治理。供中央面板（树视图）。"""
    labels = _load_labels()
    nodes = []
    for n in _all_profile_names():
        own = _node_layer(n).strip()
        nodes.append({"name": n, "label": _label(n, labels), "has_layer": bool(own),
                      "own_len": len(own), "governed": _constitution_governed(n),
                      "is_twin": is_main_twin(n)})
    return {"root": MAIN_AGENT, "nodes": nodes}


@app.post("/api/constitution/resync")
def api_constitution_resync():
    """从根重刷整棵树的 SOUL 宪法块（手动修复/立即收敛用；后台也会自动侦测文件变化）。"""
    return {"ok": True, "resynced": _materialize_constitution(MAIN_AGENT)}


@app.get("/api/constitution/{name}")
def api_constitution_node(name: str):
    """某节点宪法：自己那层(可编辑) + 继承来的各层(只读) + 有效全文。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    layers = _constitution_layers(name)
    resolved = _resolved_constitution(name)
    return {
        "name": name, "label": _label(name), "is_twin": is_main_twin(name),
        "own": _node_layer(name),
        "inherited": [l for l in layers if not l["is_self"]],
        "effective": resolved["effective"],
        "resolved": resolved["sections"],
        "rules": resolved["rules"],
        "governed": _constitution_governed(name),
    }


@app.post("/api/constitution/{name}")
def api_constitution_save_node(name: str, req: ConstLayerReq):
    """保存某节点自己那层，并重刷它整棵子树的 SOUL。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    if is_main_twin(name):
        raise HTTPException(400, "分身与主 agent 共享 SOUL，请去主 agent(X)改宪法层")
    _save_node_layer(name, req.text or "")
    return {"ok": True, "name": name, "resynced": _materialize_constitution(name)}


# --------------------------------------------------------------------------- #
# kanban：持久任务板（沿组织树把任务派给常驻 agent）。临时子 agent 用 hermes 原生 delegate_task。
# --------------------------------------------------------------------------- #
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_KANBAN_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}


class KanbanCreateReq(BaseModel):
    title: str
    assignee: str
    body: str | None = None
    tenant: str | None = None
    priority: int | None = None
    workspace: str | None = None
    workspace_kind: str | None = None
    workspace_path: str | None = None
    parents: list[str] = []
    idempotency_key: str | None = None
    max_runtime: str | None = None
    max_runtime_seconds: int | None = None
    skills: list[str] = []
    goal: bool = False
    goal_mode: bool = False
    goal_max_turns: int | None = None
    triage: bool = True  # 默认停在 triage(不自动执行);前端可传 false 直接进 ready


def _kanban(args, timeout: int = 30, board: str | None = None):
    """跑 hermes kanban；缺 db 时先 init 再重试。"""
    prefix = ["kanban"]
    if board:
        if not _BOARD_RE.match(board):
            raise HTTPException(400, "非法 board")
        prefix += ["--board", board]
    rc, out, err = run_hermes([*prefix, *args], timeout=timeout)
    if rc != 0 and "kanban.db" in (err + out):
        run_hermes([*prefix, "init"], timeout=timeout)
        rc, out, err = run_hermes([*prefix, *args], timeout=timeout)
    return rc, out, err


@app.get("/api/kanban")
def api_kanban_list(archived: bool = False, status: str | None = None, assignee: str | None = None, board: str | None = None):
    args = ["list", "--json"]
    if archived:
        args.append("--archived")
    if status and status in _KANBAN_STATUSES:
        args += ["--status", status]
    if assignee and (assignee == "default" or _PROFILE_ID_RE.match(assignee)):
        args += ["--assignee", assignee]
    rc, out, err = _kanban(args, board=board)
    if rc != 0:
        return {"tasks": [], "note": (err or out or "kanban 不可用").strip()[:200]}
    try:
        tasks = json.loads(out) if out.strip() else []
    except ValueError:
        tasks = []
    return {"tasks": tasks}


@app.post("/api/kanban/create")
def api_kanban_create(req: KanbanCreateReq, board: str | None = None):
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(400, "标题为空")
    if req.assignee != "default" and not _PROFILE_ID_RE.match(req.assignee):
        raise HTTPException(400, "非法 assignee")
    if not _profile_dir(req.assignee).is_dir():
        raise HTTPException(404, "assignee profile 不存在")
    if effective_killed(req.assignee):  # Kill switch 闸：不给已停用的 agent 派活
        raise HTTPException(409, f"assignee「{req.assignee}」已停用，无法派活；请先在仪表盘恢复")
    args = ["create", title, "--assignee", req.assignee, "--json"]
    if req.body:
        args += ["--body", req.body]
    if req.tenant:
        args += ["--tenant", req.tenant.strip()]
    if req.priority is not None:
        args += ["--priority", str(int(req.priority))]
    workspace = (req.workspace or "").strip()
    if not workspace and req.workspace_kind:
        kind = req.workspace_kind.strip()
        path = (req.workspace_path or "").strip()
        workspace = f"{kind}:{path}" if kind in {"dir", "worktree"} and path else kind
    if workspace:
        args += ["--workspace", workspace]
    for parent in req.parents or []:
        if not _TASK_ID_RE.match(parent or ""):
            raise HTTPException(400, f"非法 parent task id: {parent}")
        args += ["--parent", parent]
    if req.idempotency_key:
        args += ["--idempotency-key", req.idempotency_key.strip()]
    runtime = req.max_runtime or (str(req.max_runtime_seconds) if req.max_runtime_seconds else "")
    if runtime:
        args += ["--max-runtime", runtime.strip()]
    for skill in req.skills or []:
        skill = skill.strip()
        if skill:
            args += ["--skill", skill]
    if req.goal or req.goal_mode:
        args.append("--goal")
    if req.goal_max_turns is not None:
        args += ["--goal-max-turns", str(int(req.goal_max_turns))]
    if req.triage:  # 默认停在 triage：建任务不会被 gateway 调度器自动执行,需手动放行(specify)
        args.append("--triage")
    rc, out, err = _kanban(args, board=board)
    if rc != 0:
        raise HTTPException(502, f"建任务失败: {(err or out).strip()[:200]}")
    try:
        task = json.loads(out)
    except ValueError:
        task = {"raw": out.strip()[:200]}
    return {"ok": True, "task": task}


@app.post("/api/kanban/archive/{tid}")
def api_kanban_archive(tid: str, board: str | None = None):
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    rc, out, err = _kanban(["archive", tid], board=board)
    if rc != 0:
        raise HTTPException(502, f"归档失败: {(err or out).strip()[:200]}")
    return {"ok": True, "id": tid}


# ---- 单任务操作：详情 / 状态流转 / 派给(assign·reassign) / 认领 / 评论 ---- #
# 合法手动流转动词（其余状态由 claim/dispatch/系统驱动，不直接设置）。
_KANBAN_VERBS = {"promote", "block", "unblock", "schedule", "complete"}


class KanbanTransitionReq(BaseModel):
    verb: str
    reason: str | None = None


class KanbanAssignReq(BaseModel):
    assignee: str  # profile 名，或 'none' 取消指派
    reclaim: bool = False  # running 任务改派需先释放认领


class KanbanCommentReq(BaseModel):
    text: str


@app.get("/api/kanban/show/{tid}")
def api_kanban_show(tid: str, board: str | None = None):
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    rc, out, err = _kanban(["show", tid, "--json"], board=board)
    if rc != 0:
        raise HTTPException(502, f"读取任务失败: {(err or out).strip()[:200]}")
    try:
        raw = json.loads(out) if out.strip() else {}
    except ValueError:
        return {"task": {"raw": out.strip()[:400]}}
    # CLI show --json = {task:{...}, comments, events, parents, children, runs}；
    # 摊平：把 task 字段提到顶层，comments/events/parents/children 作为同级保留。
    inner = raw.get("task") if isinstance(raw, dict) else None
    t = dict(inner) if isinstance(inner, dict) else (dict(raw) if isinstance(raw, dict) else {})
    if isinstance(raw, dict):
        for k in ("comments", "events", "parents", "children", "runs", "latest_summary"):
            if k in raw:
                t[k] = raw[k]
    return {"task": t}


@app.post("/api/kanban/transition/{tid}")
def api_kanban_transition(tid: str, req: KanbanTransitionReq, board: str | None = None):
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    verb = (req.verb or "").strip()
    if verb not in _KANBAN_VERBS:
        raise HTTPException(400, f"非法动词「{verb}」；仅 {sorted(_KANBAN_VERBS)}")
    reason = (req.reason or "").strip()
    if verb == "complete":
        args = ["complete", tid] + (["--result", reason] if reason else [])
    else:
        args = [verb, tid] + ([reason] if reason else [])
    rc, out, err = _kanban(args, board=board)
    if rc != 0:
        raise HTTPException(502, f"{verb} 失败: {(err or out).strip()[:200]}")
    return {"ok": True, "id": tid, "verb": verb}


@app.post("/api/kanban/assign/{tid}")
def api_kanban_assign(tid: str, req: KanbanAssignReq, board: str | None = None):
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    who = (req.assignee or "").strip()
    if who != "none":
        if who != "default" and not _PROFILE_ID_RE.match(who):
            raise HTTPException(400, "非法 assignee")
        if not _profile_dir(who).is_dir():
            raise HTTPException(404, "assignee profile 不存在")
        if effective_killed(who):  # Kill switch 闸：不给已停用的 agent 派活
            raise HTTPException(409, f"assignee「{who}」已停用，无法派活；请先恢复")
    # reclaim=改派 running 任务（先释放认领）→ reassign --reclaim；否则 assign 即可
    args = (["reassign", tid, who, "--reclaim"] if req.reclaim else ["assign", tid, who])
    rc, out, err = _kanban(args, board=board)
    if rc != 0:
        raise HTTPException(502, f"指派失败: {(err or out).strip()[:200]}")
    return {"ok": True, "id": tid, "assignee": who}


@app.post("/api/kanban/claim/{tid}")
def api_kanban_claim(tid: str, board: str | None = None):
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    rc, out, err = _kanban(["claim", tid], board=board)
    if rc != 0:
        raise HTTPException(502, f"认领失败: {(err or out).strip()[:200]}")
    return {"ok": True, "id": tid, "workspace": out.strip()[:300]}


@app.post("/api/kanban/comment/{tid}")
def api_kanban_comment(tid: str, req: KanbanCommentReq, board: str | None = None):
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "评论为空")
    rc, out, err = _kanban(["comment", tid, text], board=board)
    if rc != 0:
        raise HTTPException(502, f"评论失败: {(err or out).strip()[:200]}")
    return {"ok": True, "id": tid}


# ---- 多看板 / 依赖 / 运行可视 / 诊断 / 收回 / 元信息（hermes kanban CLI 薄封装）---- #
# 注：附件、运行 inspect、运行 terminate 是 plugin_api 引擎专属（CLI 无），此处不提供。
class KanbanBoardReq(BaseModel):
    slug: str = ""  # 路径已带 slug 的端点（rename）无需 body 再传；create 用正则校验非空
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    switch: bool = True


class KanbanLinkReq(BaseModel):
    parent: str
    child: str


@app.get("/api/kanban/boards")
def api_kanban_boards():
    rc, out, err = _kanban(["boards", "list"])
    boards, current = [], None
    for raw in (out or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("current board:"):
            current = line.split(":", 1)[1].strip()
            continue
        marked = raw.lstrip().startswith("●")
        body = raw.lstrip(" \t●○*•-")
        parts = re.split(r"\s{2,}", body.strip())
        if not parts or parts[0].upper() == "SLUG" or not _BOARD_RE.match(parts[0]):
            continue
        boards.append({"slug": parts[0], "name": parts[1] if len(parts) > 1 else parts[0],
                       "counts": parts[2] if len(parts) > 2 else "", "current": marked})
    if current is None and boards:
        current = next((b["slug"] for b in boards if b["current"]), boards[0]["slug"])
    return {"boards": boards, "current": current}


@app.post("/api/kanban/boards/create")
def api_kanban_boards_create(req: KanbanBoardReq):
    slug = (req.slug or "").strip().lower()
    if not _BOARD_RE.match(slug):
        raise HTTPException(400, "非法 slug（小写字母/数字/连字符）")
    args = ["boards", "create", slug]
    if req.name:
        args += ["--name", req.name.strip()[:80]]
    if req.description:
        args += ["--description", req.description.strip()[:300]]
    if req.icon:
        args += ["--icon", req.icon.strip()[:8]]
    if req.switch:
        args.append("--switch")
    rc, out, err = _kanban(args)
    if rc != 0:
        raise HTTPException(502, f"建看板失败: {(err or out).strip()[:200]}")
    return {"ok": True, "slug": slug}


@app.post("/api/kanban/boards/switch/{slug}")
def api_kanban_boards_switch(slug: str):
    if not _BOARD_RE.match(slug):
        raise HTTPException(400, "非法 slug")
    rc, out, err = _kanban(["boards", "switch", slug])
    if rc != 0:
        raise HTTPException(502, f"切换看板失败: {(err or out).strip()[:200]}")
    return {"ok": True, "current": slug}


@app.post("/api/kanban/boards/rename/{slug}")
def api_kanban_boards_rename(slug: str, req: KanbanBoardReq):
    if not _BOARD_RE.match(slug):
        raise HTTPException(400, "非法 slug")
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "新名称为空")
    rc, out, err = _kanban(["boards", "rename", slug, name[:80]])
    if rc != 0:
        raise HTTPException(502, f"重命名失败: {(err or out).strip()[:200]}")
    return {"ok": True}


@app.post("/api/kanban/boards/rm/{slug}")
def api_kanban_boards_rm(slug: str):
    if not _BOARD_RE.match(slug):
        raise HTTPException(400, "非法 slug")
    if slug == "default":
        raise HTTPException(400, "default 看板不可删除")
    # 默认归档到 boards/_archived/（可恢复），不加 --delete
    rc, out, err = _kanban(["boards", "rm", slug])
    if rc != 0:
        raise HTTPException(502, f"删除看板失败: {(err or out).strip()[:200]}")
    return {"ok": True}


@app.post("/api/kanban/link")
def api_kanban_link(req: KanbanLinkReq, board: str | None = None):
    if not (_TASK_ID_RE.match(req.parent or "") and _TASK_ID_RE.match(req.child or "")):
        raise HTTPException(400, "非法任务 id")
    rc, out, err = _kanban(["link", req.parent, req.child], board=board)
    if rc != 0:
        raise HTTPException(502, f"加依赖失败: {(err or out).strip()[:200]}")
    return {"ok": True}


@app.post("/api/kanban/unlink")
def api_kanban_unlink(req: KanbanLinkReq, board: str | None = None):
    if not (_TASK_ID_RE.match(req.parent or "") and _TASK_ID_RE.match(req.child or "")):
        raise HTTPException(400, "非法任务 id")
    rc, out, err = _kanban(["unlink", req.parent, req.child], board=board)
    if rc != 0:
        raise HTTPException(502, f"删依赖失败: {(err or out).strip()[:200]}")
    return {"ok": True}


@app.get("/api/kanban/runs/{tid}")
def api_kanban_runs(tid: str, board: str | None = None):
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    rc, out, err = _kanban(["runs", tid, "--json"], board=board)
    if rc != 0:
        return {"runs": [], "note": (err or out).strip()[:200]}
    try:
        return {"runs": json.loads(out) if out.strip() else []}
    except ValueError:
        return {"runs": []}


@app.get("/api/kanban/log/{tid}")
def api_kanban_log(tid: str, tail: int = 4000, board: str | None = None):
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    n = max(100, min(int(tail), 200000))
    rc, out, err = _kanban(["log", tid, "--tail", str(n)], board=board)
    return {"log": (out or err or "").strip()[-n:]}


@app.get("/api/kanban/diagnostics")
def api_kanban_diagnostics(board: str | None = None):
    rc, out, err = _kanban(["diagnostics", "--json"], board=board)
    if rc != 0:
        return {"diagnostics": [], "note": (err or out).strip()[:200]}
    try:
        return {"diagnostics": json.loads(out) if out.strip() else []}
    except ValueError:
        return {"diagnostics": []}


@app.post("/api/kanban/reclaim/{tid}")
def api_kanban_reclaim(tid: str, board: str | None = None):
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    rc, out, err = _kanban(["reclaim", tid], board=board)
    if rc != 0:
        raise HTTPException(502, f"收回失败: {(err or out).strip()[:200]}")
    return {"ok": True, "id": tid}


@app.get("/api/kanban/assignees")
def api_kanban_assignees(board: str | None = None):
    rc, out, err = _kanban(["assignees", "--json"], board=board)
    if rc != 0:
        return {"assignees": []}
    try:
        return {"assignees": json.loads(out) if out.strip() else []}
    except ValueError:
        return {"assignees": []}


@app.get("/api/kanban/stats")
def api_kanban_stats(board: str | None = None):
    rc, out, err = _kanban(["stats", "--json"], board=board)
    if rc != 0:
        return {"stats": {}}
    try:
        return {"stats": json.loads(out) if out.strip() else {}}
    except ValueError:
        return {"stats": {}}


@app.post("/api/kanban/specify/{tid}")
def api_kanban_specify(tid: str, board: str | None = None):
    """放行 triage 任务：specifier 细化规格并推进到 todo（之后进入正常调度流）。"""
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    rc, out, err = _kanban(["specify", tid], timeout=120, board=board)
    if rc != 0:
        raise HTTPException(502, f"放行失败: {(err or out).strip()[:200]}")
    return {"ok": True, "id": tid}


@app.get("/api/kanban/gateway")
def api_kanban_gateway():
    """gateway 调度器状态：gateway 在跑 = 「就绪」任务会被自动认领并执行。
    gateway status 输出含一段 plist，这里只取人类可读摘要行 + 推断在跑与否。"""
    rc, out, err = run_hermes(["gateway", "status"], timeout=15)
    text = (out or "") + (("\n" + err) if err else "")
    lines = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s in "{}" or s.endswith(";"):  # 跳过 plist 主体(键值行都以 ; 结尾)
            continue
        if s[0] in "✓⚠✗❌•" or any(k in s.lower() for k in ("gateway", "service", "running", "loaded", "stopped")):
            lines.append(s[:120])
    low = text.lower()
    negative = any(x in low for x in (
        "gateway is not running",
        "gateway not running",
        "not loaded",
        "is stopped",
        "failed",
    ))
    positive = any(x in low for x in (
        "gateway is running",
        "is loaded",
        "service is running",
        "loaded and running",
    ))
    running = positive and not negative
    return {"running": running, "summary": lines[:6]}


# ---- dispatch（跑一次 / 预演）+ daemon（常驻自动派发）控制 ---- #
KANBAN_DAEMON_PID = HERE / "kanban-daemon.pid"
KANBAN_DAEMON_LOG = HERE / "kanban-daemon.log"


def _daemon_pid():
    """返回在运行的 daemon pid（pidfile 指向且进程存活），否则 None。"""
    try:
        pid = int(KANBAN_DAEMON_PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


@app.post("/api/kanban/dispatch")
def api_dispatch(dry_run: bool = False, max: int = 8, board: str | None = None):
    """派发：把 ready 的任务交给 worker 执行。dry_run=只预演不真跑。
    ⚠ 非 dry-run = worker agent 会自主执行任务（调工具、有副作用）。"""
    n = max if max and max > 0 else 8
    args = ["dispatch", "--json", "--max", str(min(n, 20))] + (["--dry-run"] if dry_run else [])
    rc, out, err = _kanban(args, timeout=60, board=board)
    if rc != 0:
        raise HTTPException(502, f"dispatch 失败: {(err or out).strip()[:200]}")
    try:
        result = json.loads(out) if out.strip() else {}
    except ValueError:
        result = {"raw": out.strip()[:400]}
    return {"ok": True, "dry_run": dry_run, "result": result}


@app.post("/api/kanban/decompose/{tid}")
def api_kanban_decompose(tid: str, author: str | None = None, board: str | None = None):
    """把 triage 任务交给 Hermes 官方 decomposer 扇出成子任务图。"""
    if not _TASK_ID_RE.match(tid):
        raise HTTPException(400, "非法任务 id")
    args = ["decompose", tid, "--json"]
    if author:
        args += ["--author", author.strip()[:80]]
    rc, out, err = _kanban(args, timeout=180, board=board)
    if rc != 0:
        raise HTTPException(502, f"拆解失败: {(err or out).strip()[:300]}")
    try:
        result = json.loads(out) if out.strip() else {}
    except ValueError:
        result = {"raw": out.strip()[:800]}
    return {"ok": True, "id": tid, "result": result}


@app.get("/api/kanban/daemon")
def api_daemon_status():
    pid = _daemon_pid()
    return {"running": pid is not None, "pid": pid}


@app.post("/api/kanban/daemon/start")
def api_daemon_start():
    """启动常驻 daemon（detached，每 60s 自动派发，--max 3 限速）。
    ⚠ 启动 = 被派任务会被 worker agent 自主执行。"""
    pid = _daemon_pid()
    if pid:
        return {"ok": True, "running": True, "pid": pid, "note": "已在运行"}
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    for k, v in _ROOT_ENV.items():
        env.setdefault(k, v)
    try:
        logf = open(KANBAN_DAEMON_LOG, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [HERMES_BIN, "kanban", "daemon", "--interval", "60", "--max", "3"],
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env, close_fds=True,
        )
        KANBAN_DAEMON_PID.write_text(str(proc.pid), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"启动 daemon 失败: {e}")
    return {"ok": True, "running": True, "pid": proc.pid}


@app.post("/api/kanban/daemon/stop")
def api_daemon_stop():
    pid = _daemon_pid()
    if not pid:
        try:
            KANBAN_DAEMON_PID.unlink()
        except OSError:
            pass
        return {"ok": True, "running": False, "note": "本就没在运行"}
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        KANBAN_DAEMON_PID.unlink()
    except OSError:
        pass
    return {"ok": True, "running": False}


@app.get("/api/profile/{name}")
def api_profile(name: str):
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    pdir = _profile_dir(name)
    if not pdir.is_dir():
        raise HTTPException(404, "profile 不存在")

    soul = ""
    sp = pdir / "SOUL.md"
    if sp.is_file():
        try:
            soul = sp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            soul = ""

    all_sessions = sessions_from_state_db(name, limit=50)
    sessions = all_sessions[:8]
    sessions_note = None

    role = agent_role(name)
    model, provider = read_effective_config_model(name)
    return {
        "name": name,
        "role": role,
        "in_network": role != "none",
        "killed": name in _load_killed(),
        "effective_killed": effective_killed(name),
        "main_twin": is_main_twin(name),
        "parent": effective_parent(name),
        "symlinks": skill_symlinks(name),
        "model": model,
        "provider": provider,
        "soul": soul,
        "skills": profile_skills(name),
        "skill_count": count_skills(name),
        "sessions": sessions,
        "session_count": len(all_sessions),
        "sessions_note": sessions_note,
        "is_default": name == "default",
    }


# --------------------------------------------------------------------------- #
# Profile 面板扩展：影响范围 / 备份历史 / 恢复 / 子任务 —— 4 个端点喂给重构后的 6 分区面板。
# 全部读已有数据：hierarchy + skill_inherit_off + config_inherit_block + 各种 .dashbak + kanban。
# --------------------------------------------------------------------------- #
def _profile_impact(name: str) -> dict:
    """name 改了，哪些后代会跟着变？三个维度：
       1. config_inherit: 后代里"没把任何键 block 掉"的——会接受 name 通过沿树继承下发的可继承键
       2. external_dirs: 后代里 converted=true 且未 skill_inherit_off 的——name 的 skills/ 是它们的祖先目录
       3. symlink_consumers: 谁的 skills/ 下有 symlink 指向 name 的某个技能（横向共享）
       注：知识库已改全局（所有 agent 共享一个库），不再随组织树继承，故不计入影响范围。"""
    nodes = build_tree()["nodes"]
    descendants = sorted(_descendants(name, nodes))

    # 1 + 2: descendants 按沿树继承通用规则
    block_all = _load_config_block_all()
    skill_off = _load_skill_inherit_off()
    config_inherit = []
    external_dirs = []
    for d in descendants:
        # config 继承：descendants 都会通过 _effective_config 链拿到 name 的可继承键，
        # 除非中间某个节点 block 掉某键（不过 block 是单键级别，不全屏蔽；这里我们简化为"有沿树链路"）
        config_inherit.append({
            "name": d,
            "blocks_count": len(block_all.get(d, [])),  # 自己 block 了几个键
        })
        # external_dirs: 默认继承——除非显式 opt-out（与 _materialize_config 同口径）
        if d not in skill_off:
            external_dirs.append(d)

    # 3: 谁横向共享 name 的技能（symlink，不一定是树后代）
    symlink_consumers = []
    for nm in nodes:
        if nm == name:
            continue
        sls = skill_symlinks(nm)
        used = [s["name"] for s in sls if s.get("target_profile") == name]
        if used:
            symlink_consumers.append({"name": nm, "skills": used})

    return {
        "name": name,
        "descendants": descendants,
        "config_inherit": config_inherit,
        "external_dirs": external_dirs,
        "symlink_consumers": symlink_consumers,
        "summary": {
            "descendants_count": len(descendants),
            "config_affected": len(config_inherit),
            "external_dirs_count": len(external_dirs),
            "symlink_consumer_count": len(symlink_consumers),
        },
    }


@app.get("/api/profile/{name}/impact")
def api_profile_impact(name: str):
    """改这个 profile 会影响谁？沿组织树继承 + 横向 symlink 共享 + vault 都算上。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    return _profile_impact(name)


_BACKUP_PATTERNS = (
    ("config.yaml.dashbak", "config", "落位/继承前 config 备份"),
    ("SOUL.md.constibak", "soul", "宪法注入前 SOUL 备份"),
)


def _list_backups(name: str) -> list:
    """扫 profile 目录下的 .dashbak / .constibak（知识库已改全局，备份不再落在 profile 内）。"""
    out = []
    pd = _profile_dir(name)
    for fname, kind, summary in _BACKUP_PATTERNS:
        p = pd / fname
        if p.is_file():
            try:
                st = p.stat()
                out.append({"kind": kind, "file": fname, "summary": summary,
                            "size": st.st_size, "mtime": st.st_mtime,
                            "restore_target": fname.removesuffix(".dashbak").removesuffix(".constibak")})
            except OSError:
                pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    now = time.time()
    for x in out:
        ago = max(0, int(now - x["mtime"]))
        if ago < 60:
            x["ago"] = f"{ago} 秒前"
        elif ago < 3600:
            x["ago"] = f"{ago // 60} 分钟前"
        elif ago < 86400:
            x["ago"] = f"{ago // 3600} 小时前"
        else:
            x["ago"] = f"{ago // 86400} 天前"
        x["when"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x["mtime"]))
    return out


@app.get("/api/profile/{name}/backups")
def api_profile_backups(name: str):
    """这个 profile 所有 .dashbak / .constibak / vault note .dashbak 的列表（按时间倒序）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    return {"name": name, "backups": _list_backups(name)}


class RestoreReq(BaseModel):
    file: str  # 必须匹配 _list_backups 返回的 file 字段


@app.post("/api/profile/{name}/restore")
def api_profile_restore(name: str, req: RestoreReq):
    """从某个 .dashbak / .constibak 把内容写回原文件。先把当前内容再备一份（避免单次恢复就丢失现状）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    pd = _profile_dir(name)
    if not pd.is_dir():
        raise HTTPException(404, "profile 不存在")
    # 找匹配的备份
    backups = _list_backups(name)
    match = next((b for b in backups if b["file"] == req.file), None)
    if not match:
        raise HTTPException(404, f"备份不存在: {req.file}")
    bak_path = pd / req.file
    target = pd / match["restore_target"]
    if not bak_path.is_file():
        raise HTTPException(404, f"备份文件已消失: {req.file}")
    # 把当前文件再备一份（防误操作），用 .pre-restore 后缀
    if target.is_file():
        try:
            pre = target.with_suffix(target.suffix + ".pre-restore")
            pre.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    try:
        target.write_text(bak_path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"恢复失败: {e}")
    return {"ok": True, "restored": str(target.relative_to(pd)),
            "from": req.file, "pre_restore_saved": target.with_suffix(target.suffix + ".pre-restore").is_file()}


@app.get("/api/profile/{name}/subtasks")
def api_profile_subtasks(name: str):
    """这个 profile 担任 assignee 的 kanban 任务（用于 profile 面板的子任务卡区）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    rc, out, _ = _kanban(["list", "--json"])
    if rc != 0:
        return {"name": name, "tasks": [], "note": "kanban 不可用"}
    try:
        tasks = json.loads(out) if out.strip() else []
    except ValueError:
        tasks = []
    mine = [t for t in tasks if t.get("assignee") == name]
    return {"name": name, "tasks": mine, "total": len(mine)}


@app.get("/api/sessions/{name}")
def api_sessions(name: str):
    """某 profile 的会话列表（= 对话框）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    hidden_ids = _session_archive_hidden_ids(name)
    db_sessions = sessions_from_state_db(name, limit=50)
    live_sessions = {
        sid: info for sid, info in active_pty_sessions_for_profile(name).items()
        if sid not in hidden_ids
    }

    merged: dict[str, dict] = {}
    order: list[str] = []
    for session in db_sessions:
        sid = session.get("id")
        if not sid or sid in merged:
            continue
        merged[sid] = session
        order.append(sid)
    for sid, runtime_info in live_sessions.items():
        if sid not in merged:
            merged[sid] = {
                "id": sid,
                "title": "后台 PTY 会话",
                "preview": "该运行时仍在后台运行，但当前后端会话索引未列出它。",
                "started_at": None,
                "message_count": 0,
                "source": "runtime",
            }
            order.insert(0, sid)
        merged[sid].update(runtime_info)
        merged[sid]["ended_at"] = None
        source = str(merged[sid].get("source") or "")
        if "runtime" not in source:
            merged[sid]["source"] = f"{source}+runtime" if source else "runtime"
    if live_sessions:
        live_order = sorted(
            (sid for sid in order if sid in live_sessions),
            key=lambda sid: (
                -int(live_sessions[sid].get("runtime_subscribers") or 0),
                float(live_sessions[sid].get("runtime_idle_seconds") or 0),
            ),
        )
        rest_order = [sid for sid in order if sid not in live_sessions]
        order = live_order + rest_order
    sessions = [merged[sid] for sid in order]
    return {
        "name": name,
        "sessions": sessions,
        "note": None,
    }


def _health_snippet(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = _ANSI_RE.sub("", text).replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?i)(api[_-]?key|token|secret|authorization)(['\"\s:=]+)[^,'\"\s}]{8,}", r"\1\2[redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}", r"\1[redacted]", text)
    return _session_preview(text, 100)


def _message_size(row: sqlite3.Row) -> int:
    return (
        len(row["content"] or "")
        + len(row["tool_calls"] or "")
        + len(row["reasoning"] or "")
        + len(row["reasoning_content"] or "")
        + len(row["reasoning_details"] or "")
        + len(row["codex_reasoning_items"] or "")
        + len(row["codex_message_items"] or "")
    )


def _session_parent_depth(conn: sqlite3.Connection, session_id: str) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT parent_session_id FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    parent = str(row["parent_session_id"] or "") if row else ""
    current = parent
    seen = {session_id}
    depth = 0
    while current and current not in seen and depth < 100:
        depth += 1
        seen.add(current)
        row = conn.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?",
            (current,),
        ).fetchone()
        if not row:
            break
        current = str(row["parent_session_id"] or "")
    return depth, parent or None


def _session_health_level(stats: dict) -> tuple[str, list[str], list[str]]:
    reasons: list[str] = []
    actions: list[str] = []

    estimated_tokens = int(stats.get("estimated_tokens") or 0)
    context_window = max(1, int(stats.get("context_window") or _SESSION_HEALTH_FALLBACK_CONTEXT))
    context_ratio = estimated_tokens / context_window
    tool_tokens = int(stats.get("tool_tokens") or 0)
    tool_ratio = tool_tokens / context_window

    level = "ok"
    for candidate in ("critical", "bloated", "watch"):
        t = _SESSION_HEALTH_THRESHOLDS[candidate]
        if context_ratio >= t["context_ratio"]:
            level = candidate
            reasons.append(
                f"estimated context {estimated_tokens:,}/{context_window:,} tokens "
                f"({context_ratio:.0%}) >= {t['context_ratio']:.0%}"
            )
            break

    if tool_ratio >= _SESSION_HEALTH_TOOL_HEAVY_RATIO:
        reasons.append(
            f"tool output is heavy: ~{tool_tokens:,} tokens ({tool_ratio:.0%} of model window)"
        )

    if level == "ok":
        if not reasons:
            reasons.append(
                f"estimated context {estimated_tokens:,}/{context_window:,} tokens "
                f"({context_ratio:.0%}); within Session Governor V2 limits"
            )
        actions.append("继续打开真实终端即可。")
    elif level == "watch":
        actions.append("建议在继续长任务前做一次阶段总结，避免工具输出继续堆积。")
        actions.append("需要实时处理时仍可继续打开真实终端。")
    elif level == "bloated":
        actions.append("建议让当前终端输出精简 handoff，然后新开 session 继续。")
        actions.append("避免继续把大 read_file、terminal、patch diff 原文留在同一会话里。")
    else:
        actions.append("此 session 已过肥；建议生成 handoff 后新开 session。")
        actions.append("继续打开仍允许，但很可能频繁触发压缩。")
    return level, reasons, actions


def _compression_config_for_health(name: str, context_window: int | None = None) -> dict:
    cfg = _read_config(name)
    comp = cfg.get("compression") if isinstance(cfg.get("compression"), dict) else {}
    return {
        "enabled": comp.get("enabled", True),
        "threshold": comp.get("threshold"),
        "target_ratio": comp.get("target_ratio"),
        "protect_last_n": int(comp.get("protect_last_n") or 20),
        "hygiene_hard_message_limit": comp.get("hygiene_hard_message_limit"),
        "protect_first_n": comp.get("protect_first_n"),
        "abort_on_summary_failure": comp.get("abort_on_summary_failure"),
        "context_window": context_window,
    }


@app.get("/api/session-health/{name}/{session_id}")
def api_session_health(name: str, session_id: str):
    """Session Governor V2: model-aware, read-only context diagnostics.

    This endpoint never mutates Hermes state. It only estimates context pressure
    from the selected profile's SQLite session rows and returns short snippets.
    """
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "非法会话 ID")
    profile_dir = _profile_dir(name)
    if not profile_dir.is_dir():
        raise HTTPException(404, "profile 不存在")
    db = profile_dir / "state.db"
    if not db.is_file():
        raise HTTPException(404, "state.db 不存在")

    comp = _compression_config_for_health(name, _SESSION_HEALTH_FALLBACK_CONTEXT)
    protect_last_n = max(1, min(int(comp.get("protect_last_n") or 20), 200))
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.5)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise HTTPException(404, f"state.db 不可读: {exc}") from exc
    try:
        session = conn.execute(
            """
            SELECT id, title, model, model_config, system_prompt, parent_session_id,
                   message_count, input_tokens, output_tokens, ended_at, end_reason
            FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if not session:
            raise HTTPException(404, "session 不存在")
        rows = conn.execute(
            """
            SELECT id, role, content, tool_calls, tool_name, reasoning, reasoning_content,
                   reasoning_details, codex_reasoning_items, codex_message_items
            FROM messages
            WHERE session_id = ? AND active = 1
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()
        parent_depth, parent_session_id = _session_parent_depth(conn, session_id)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        return {
            "name": name,
            "session_id": session_id,
            "level": "unknown",
            "reasons": [f"DB schema not supported: {exc}"],
            "actions": ["无法读取会话健康信息；仍可复制启动命令手动打开。"],
            "stats": None,
            "compression": comp,
            "top_messages": [],
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass

    model_meta = _model_for_id(str(session["model"] or "")) or {}
    context_window = int(model_meta.get("context_window") or _SESSION_HEALTH_FALLBACK_CONTEXT)
    comp = _compression_config_for_health(name, context_window)
    protect_last_n = max(1, min(int(comp.get("protect_last_n") or 20), 200))
    role_stats: dict[str, dict[str, int]] = {}
    total_chars = len(session["system_prompt"] or "")
    if session["system_prompt"]:
        role_stats["system"] = {"count": 1, "chars": len(session["system_prompt"] or "")}
    assistant_tool_call_chars = 0
    top_messages = []
    for row in rows:
        role = str(row["role"] or "unknown")
        content_chars = len(row["content"] or "")
        tool_call_chars = len(row["tool_calls"] or "")
        reasoning_chars = (
            len(row["reasoning"] or "")
            + len(row["reasoning_content"] or "")
            + len(row["reasoning_details"] or "")
            + len(row["codex_reasoning_items"] or "")
            + len(row["codex_message_items"] or "")
        )
        size = content_chars + tool_call_chars + reasoning_chars
        bucket = role_stats.setdefault(role, {"count": 0, "chars": 0})
        bucket["count"] += 1
        bucket["chars"] += content_chars
        total_chars += size
        if role == "assistant":
            assistant_tool_call_chars += tool_call_chars
        kind = row["tool_name"] if role == "tool" and row["tool_name"] else ("tool_call" if tool_call_chars else role)
        top_messages.append({
            "id": int(row["id"]),
            "role": role,
            "chars": size,
            "kind": kind,
            "snippet": _health_snippet(row["content"] or row["tool_calls"] or row["reasoning"]),
        })

    tail_rows = rows[-protect_last_n:]
    recent_tail_chars = sum(_message_size(row) for row in tail_rows)
    tool_chars = int(role_stats.get("tool", {}).get("chars", 0))
    estimated_tokens = (total_chars + 3) // 4
    tool_tokens = (tool_chars + 3) // 4
    recent_tail_tokens = (recent_tail_chars + 3) // 4
    context_ratio = estimated_tokens / max(1, context_window)
    stats = {
        "message_count": len(rows),
        "db_message_count": int(session["message_count"] or 0),
        "role_stats": role_stats,
        "total_chars": total_chars,
        "estimated_tokens": estimated_tokens,
        "context_window": context_window,
        "context_ratio": context_ratio,
        "context_percent": round(context_ratio * 100, 1),
        "tool_chars": tool_chars,
        "tool_tokens": tool_tokens,
        "assistant_tool_call_chars": assistant_tool_call_chars,
        "recent_tail_count": len(tail_rows),
        "recent_tail_chars": recent_tail_chars,
        "recent_tail_tokens": recent_tail_tokens,
        "parent_session_id": parent_session_id,
        "parent_chain_depth": parent_depth,
        "model": session["model"],
        "input_tokens": int(session["input_tokens"] or 0),
        "output_tokens": int(session["output_tokens"] or 0),
    }
    level, reasons, actions = _session_health_level(stats)
    top_messages.sort(key=lambda item: item["chars"], reverse=True)
    return {
        "name": name,
        "session_id": session_id,
        "title": session["title"],
        "level": level,
        "reasons": reasons,
        "actions": actions,
        "stats": stats,
        "compression": comp,
        "thresholds": _SESSION_HEALTH_THRESHOLDS,
        "token_estimate_method": "ceil(chars/4), matching Hermes rough pre-flight estimation",
        "top_messages": top_messages[:_SESSION_HEALTH_TOP_N],
    }


class TerminalOpenReq(BaseModel):
    session_id: str | None = None


@app.post("/api/terminal/{name}/open")
def api_open_terminal(name: str, req: TerminalOpenReq):
    """Open a real macOS Terminal running `hermes -p <agent> chat`.

    The dashboard no longer starts the browser-embedded `/ws/chat` PTY from the
    formal conversation page. This endpoint is only a convenience launcher; the
    returned command is also shown in the UI for manual copy/run fallback.
    """
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    if effective_killed(name):
        raise HTTPException(409, "该 Agent 已停用（或其上级被停用），请恢复后再打开终端")
    session_id = (req.session_id or "").strip() or None
    if session_id and not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "非法会话 ID")
    command = _terminal_chat_command(name, session_id)
    script = _terminal_chat_script(name, session_id)
    script_path = _open_macos_terminal(script, name, session_id)
    return {"ok": True, "command": command, "script": str(script_path)}


# ── Dashboard 自身配置 API ──────────────────────────────────────────

class DashboardConfigReq(BaseModel):
    terminal_app: str | None = None


@app.get("/api/dashboard/config")
def api_get_dashboard_config():
    """读 dashboard 自身偏好（终端应用等）。"""
    return {
        "terminal_app": _dashboard_terminal_app(),
        "available_terminal_apps": _dashboard_available_terminal_apps(),
    }


@app.post("/api/dashboard/config")
def api_set_dashboard_config(req: DashboardConfigReq):
    """写 dashboard 自身偏好。"""
    cfg = _load_dashboard_config()
    if req.terminal_app is not None:
        ta = req.terminal_app.strip()
        apps = [a["name"] for a in _dashboard_available_terminal_apps()]
        if ta and ta not in apps:
            raise HTTPException(400, f"未知终端应用: {ta}。可选: {', '.join(apps)}")
        cfg["terminal_app"] = ta
    _save_dashboard_config(cfg)
    return cfg


@app.get("/api/skills/{name}")
def api_skills(name: str):
    """该 profile 的技能名集合（reparent/共享对话框用来 diff 父子技能差异）。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    items, seen = [], set()
    for lst in profile_skills(name).values():
        for sk in lst:
            nm = sk.get("name")
            if nm and nm not in seen and _SKILL_ID_RE.match(nm):
                seen.add(nm)
                items.append({"name": nm})
    items.sort(key=lambda x: x["name"])
    return {"name": name, "skills": items}


# ---- per-agent 技能管理：成员制（自有/继承 分类 + 装 install / 卸 uninstall / 解除 继承） ----
#   配置 = 成员（装了哪些），不是开关：技能装上即按需/模型自动触发（渐进式披露），无需逐个启停。
_SKILL_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")  # install 标识（id/路径段；URL 另判）


@app.get("/api/agent-skills/{name}")
def api_agent_skills(name: str):
    """这个 agent 的技能集：每条标注 自有 / 继承（symlink 共享 或 external_dirs 沿树继承）+ 来源。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    items = []
    for cat, lst in profile_skills(name).items():
        for sk in lst:
            nm = sk.get("name")
            if not nm:
                continue
            items.append({
                "name": nm,
                "desc": sk.get("desc", ""),
                "category": cat,
                "inherited": bool(sk.get("inherited")),
                "source": sk.get("source"),
                "via": sk.get("via", "own"),
            })
    items.sort(key=lambda x: (not x["inherited"], x["name"]))
    return {"name": name, "skills": items, "converted": (_profile_dir(name) / ".no-bundled-skills").exists()}


class InstallReq(BaseModel):
    identifier: str


@app.post("/api/agent-skills/{name}/install")
def api_skill_install(name: str, req: InstallReq):
    """给该 agent 装一个技能：hermes skills install <identifier> --yes。
    ⚠ 联网拉取 + 安全扫描；前端须二次确认。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    ident = (req.identifier or "").strip()
    is_url = ident.startswith("http://") or ident.startswith("https://")
    if not ident or (not is_url and not _SKILL_IDENT_RE.match(ident)):
        raise HTTPException(400, "非法 identifier（应为技能 id 如 owner/skills/name，或 SKILL.md 的 URL）")
    args = (["-p", name] if name != "default" else []) + ["skills", "install", ident, "--yes"]
    rc, out, err = run_hermes(args, timeout=180)
    if rc != 0:
        raise HTTPException(502, f"安装失败：{(err or out or '退出码 ' + str(rc)).strip()[:300]}")
    return {"ok": True, "identifier": ident, "output": (out or "").strip()[:400]}


class UninstallReq(BaseModel):
    skill: str


@app.post("/api/agent-skills/{name}/uninstall")
def api_skill_uninstall(name: str, req: UninstallReq):
    """卸载该 agent 一个（hub/local 装的）技能。⚠ 破坏性；前端须二次确认。
    内置(bundled)技能卸不掉——hermes 会报错，照实返回。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _SKILL_ID_RE.match(req.skill):
        raise HTTPException(400, "非法技能名")
    args = (["-p", name] if name != "default" else []) + ["skills", "uninstall", req.skill]
    rc, out, err = run_hermes(args, timeout=60, input_text="y\n")  # 喂 y 确认
    blob = (out + err)
    if rc != 0 or "Cancelled" in blob or "not found" in blob.lower():
        raise HTTPException(502, f"卸载失败/未生效：{(err or out or '退出码 ' + str(rc)).strip()[:300]}")
    return {"ok": True, "skill": req.skill, "output": blob.strip()[:400]}


@app.delete("/api/session/{name}/{sid}")
async def api_delete_session(name: str, sid: str):
    """删除一个对话框（会话）及其上下文。破坏性操作——前端必须二次确认后才调用。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _SESSION_ID_RE.match(sid):
        raise HTTPException(400, "非法会话 ID")
    runtime_id = f"{name}:{sid}"
    runtime = _PTY_RUNTIMES.get(runtime_id)
    if runtime:
        await runtime.stop()
    else:
        _quit_screen_session(_screen_session_name(runtime_id))
    args = (["-p", name] if name != "default" else []) + ["sessions", "delete", sid, "--yes"]
    rc, out, err = await asyncio.to_thread(run_hermes, args)
    if rc != 0:
        detail = (err or out or f"hermes 退出码 {rc}").strip()[:200]
        raise HTTPException(502, f"删除失败：{detail}")
    _clear_pty_replay(runtime_id)
    return {"ok": True}


@app.websocket("/ws/chat/{name}")
async def ws_chat(ws: WebSocket, name: str):
    """把浏览器终端附着到后台常驻 PTY；断开 WS 不退出 Hermes。"""
    if not _ws_origin_ok(ws):
        await ws.close(code=1008)  # 跨站 WebSocket 劫持：握手阶段拒绝，不起进程
        return
    await ws.accept()
    if name != "default" and not _PROFILE_ID_RE.match(name):
        await ws.close(code=1008)
        return
    if name not in {r["name"] for r in build_profiles()[0]}:
        await ws.close(code=1008)
        return
    if effective_killed(name):  # Kill switch 闸：停用（或上级停用）则不起进程
        try:
            await ws.send_bytes(
                f"\r\n⛔ Agent「{name}」已停用（或其上级被停用）。请在仪表盘恢复后再开终端。\r\n".encode()
            )
        finally:
            await ws.close(code=1000)
        return

    # 查询参数：?new=1 开新会话；?resume=<sid> 恢复指定对话框；
    # ?terminal_id=<stable id> 用于页面切换后重新附着同一个后台 PTY。
    qp = ws.query_params
    fresh = qp.get("new") == "1"
    resume_sid = qp.get("resume") or None
    runtime_id = qp.get("terminal_id") or f"{name}:{resume_sid or ('new' if fresh else 'continue')}"
    replay = qp.get("replay") != "0"
    initial_rows, initial_cols = _clamp_pty_size(qp.get("rows"), qp.get("cols"))
    if not _PTY_RUNTIME_ID_RE.match(runtime_id) or not _pty_runtime_id_matches_profile(runtime_id, name):
        await ws.close(code=1008)
        return
    if resume_sid and not _SESSION_ID_RE.match(resume_sid):
        await ws.close(code=1008)
        return
    try:
        runtime = _get_or_start_pty_runtime(
            runtime_id,
            name,
            resume_sid=resume_sid,
            fresh=fresh,
            rows=initial_rows,
            cols=initial_cols,
        )
    except FileNotFoundError:
        try:
            await ws.send_bytes("找不到 hermes 可执行文件\r\n".encode())
        finally:
            await ws.close()
        return
    except ValueError:
        await ws.close(code=1008)
        return
    except Exception as exc:
        try:
            await ws.send_bytes(f"PTY 启动失败：{exc}\r\n".encode())
        finally:
            await ws.close()
        return

    # 注意：不要在"重新附着已存在的 PTY"时用 URL 参数尺寸去 resize——
    # 隐藏标签页/未完成布局的前端只能报 100x30 的兜底值，无条件 resize 会把
    # 正在运行的 TUI 强行重绘到错误宽度（边框错位/重复 prompt 的来源之一）。
    # 新建 runtime 时 _start_pty_runtime 已按参数设置尺寸；真实尺寸由前端
    # 布局完成后的 resize 消息推送。
    queue, snapshot = runtime.attach(replay=replay)

    async def pty_to_ws():
        if snapshot:
            await ws.send_bytes(snapshot)
        while True:
            data = await queue.get()
            if data is None:
                break
            try:
                await ws.send_bytes(data)
            except Exception:
                break

    async def ws_to_pty():
        while True:
            try:
                raw = await ws.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                break
            except Exception:
                break
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                obj = {"type": "input", "data": raw}
            if obj.get("type") == "ping":
                continue
            if obj.get("type") == "resize":
                rows, cols = _clamp_pty_size(obj.get("rows"), obj.get("cols"))
                runtime.propose_size(queue, rows, cols)
                continue
            if obj.get("type") == "resize_release":
                runtime.release_size(queue)
                continue
            # terminal_response：xterm 对 TUI 设备查询（光标位置/前后景色）的应答。
            # 必须送达 PTY（否则 TUI 等不到应答会反复查询，残留控制序列），
            # 但要抑制回显，避免应答本身变成可见输出。
            suppress_echo = obj.get("type") == "terminal_response"
            data = obj.get("data", "")
            if suppress_echo and not data:
                continue  # 兼容旧前端的空通知
            if not runtime.write(data, suppress_echo=suppress_echo):
                break

    reader = asyncio.create_task(pty_to_ws())
    writer = asyncio.create_task(ws_to_pty())
    try:
        await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        reader.cancel()
        writer.cancel()
        runtime.detach(queue)
        try:
            await ws.close()
        except Exception:
            pass


@app.websocket("/ws/terminal-lab/{name}")
async def ws_terminal_lab(ws: WebSocket, name: str):
    """V1 实验入口：极薄 PTY 桥。

    与正式 /ws/chat 不同，这里故意不接入 screen carrier、runtime 池、回放缓存、
    URL/文件/OAuth 事件、模型热切或标签/白板状态。一个 WebSocket 就是一条
    直接 PTY；断开即 SIGHUP/SIGTERM 结束子进程，用来验证 Terminal-faithful
    的最小交互面是否稳定。
    """
    if not _ws_origin_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    if name != "default" and not _PROFILE_ID_RE.match(name):
        await ws.close(code=1008)
        return
    if name not in {r["name"] for r in build_profiles()[0]}:
        await ws.close(code=1008)
        return
    if effective_killed(name):
        await ws.send_bytes(
            f"\r\n⛔ Agent「{name}」已停用（或其上级被停用）。请在仪表盘恢复后再开终端。\r\n".encode()
        )
        await ws.close(code=1000)
        return

    qp = ws.query_params
    rows, cols = _clamp_pty_size(qp.get("rows"), qp.get("cols"))
    resume_sid = qp.get("resume") or None
    fresh = qp.get("new") == "1"
    if resume_sid and not _SESSION_ID_RE.match(resume_sid):
        await ws.close(code=1008)
        return

    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, rows, cols)
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            _chat_argv(name, resume_sid=resume_sid, fresh=fresh),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=_chat_env(),
            close_fds=True,
        )
    except FileNotFoundError:
        os.close(master_fd)
        os.close(slave_fd)
        await ws.send_bytes("找不到 hermes 可执行文件\r\n".encode())
        await ws.close(code=1011)
        return
    except Exception as exc:
        os.close(master_fd)
        os.close(slave_fd)
        await ws.send_bytes(f"Terminal Lab 启动失败：{exc}\r\n".encode())
        await ws.close(code=1011)
        return
    finally:
        try:
            os.close(slave_fd)
        except OSError:
            pass

    os.set_blocking(master_fd, False)

    async def pty_to_ws():
        try:
            while True:
                try:
                    data = os.read(master_fd, 65536)
                except BlockingIOError:
                    if proc.poll() is not None:
                        break
                    await asyncio.sleep(0.02)
                    continue
                except OSError:
                    break
                if not data:
                    break
                try:
                    await ws.send_bytes(data)
                except Exception:
                    break
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    async def ws_to_pty():
        try:
            while True:
                msg = await ws.receive()
                msg_type = msg.get("type")
                if msg_type == "websocket.disconnect":
                    break
                raw = msg.get("bytes")
                if raw is None:
                    text = msg.get("text")
                    raw = text.encode("utf-8") if isinstance(text, str) else b""
                if not raw:
                    continue
                match = _TERMINAL_LAB_RESIZE_RE.match(raw)
                if match and match.end() == len(raw):
                    next_cols = max(1, int(match.group(1)))
                    next_rows = max(1, int(match.group(2)))
                    _set_winsize(master_fd, next_rows, next_cols)
                    continue
                try:
                    os.write(master_fd, raw)
                except OSError:
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass

    reader = asyncio.create_task(pty_to_ws())
    writer = asyncio.create_task(ws_to_pty())
    try:
        await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        reader.cancel()
        writer.cancel()
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
        except (ProcessLookupError, OSError):
            pass
        time.sleep(0.05)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        deadline = time.monotonic() + 0.75
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.025)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        try:
            proc.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            pass


# --------------------------------------------------------------------------- #
# 前端托管:web/dist(vite build 产物)直接由本后端在 :8877 服务 —— 桌面 App /
#   浏览器都只需要这一个端口,不再依赖 :5174 dev server(:5174 保留给开发热更)。
#   路由顺序:所有 /api/* 与 /ws/* 在前面定义、优先匹配;dist 挂在最后兜底。
#   改前端后要 `npm run build` 才会反映到这里(开发时仍用 :5174)。
# --------------------------------------------------------------------------- #
WEB_DIST = HERE / "web" / "dist"


# --------------------------------------------------------------------------- #
# Phase 1「换地基」：Project 领域库的只读 API(feature flag: project_domain_v1)
#   默认关闭 → 下面这段整体是个 no-op:不 import 领域层、不建库、不注册任何路由,
#   现有路由与行为一行不变。开启后新增 GET /api/projects|backends|bindings|_domain/diff。
#   开关: 环境变量 DASH_FEATURE_PROJECT_DOMAIN_V1=1,或 dashboard-config.json 的
#         {"features": {"project_domain_v1": true}}。库默认在 <dashboard>/state/domain.sqlite3。
#   回滚: 关 flag(+ 删该 sqlite 文件)。细节见 AGENTS.md「Phase 1」一节与 domain_bootstrap.py。
#   必须挂在下面 WEB_DIST 的根挂载之前,否则会被静态文件兜底吃掉。
# --------------------------------------------------------------------------- #
_DOMAIN_RUNTIME = None
try:
    import domain_bootstrap as _domain_bootstrap

    _DOMAIN_RUNTIME = _domain_bootstrap.attach_domain_api(
        app,
        state_root=HERE,
        hermes_home=HERMES_HOME,
        profiles_dir=PROFILES_DIR,
        config_loader=_load_dashboard_config,
    )
except Exception as _domain_exc:  # 领域库是旁路设施,坏掉不得影响仪表盘启动
    print(f"[project_domain_v1] 跳过：{_domain_exc!r}")


# --------------------------------------------------------------------------- #
# Phase 3B「会话接入」：Session Host 的 HTTP/SSE 接口(feature flag: session_host_v1)
#   默认关闭 → 下面这段整体是个 no-op。开启后新增 /api/conversations/* 与
#   /api/projects/{id}/conversations、/api/backends/{id}/models。Driver 注册来源 =
#   dashboard-config.json 的 backends[](AD-53);缺该键时只注册 mock。
#   开关: DASH_FEATURE_SESSION_HOST_V1=1,或 {"features":{"session_host_v1":true}}。
#   细节见 session_bootstrap.py。
# --------------------------------------------------------------------------- #
_SESSION_RUNTIME = None
try:
    import session_bootstrap as _session_bootstrap

    _SESSION_RUNTIME = _session_bootstrap.attach_session_api(
        app,
        repositories=(
            _DOMAIN_RUNTIME.unit_of_work.repositories if _DOMAIN_RUNTIME else None
        ),
        config_loader=_load_dashboard_config,
    )
except Exception as _session_exc:  # 会话 API 是旁路设施,坏掉不得影响仪表盘启动
    print(f"[session_host_v1] 跳过：{_session_exc!r}")


# --------------------------------------------------------------------------- #
# R5（外部评审 2026-09-07）：旧写接口统一鉴权。
#   会话接口走 Origin + 本地 token 两关,而这个文件里 58 条 POST/PUT/PATCH/DELETE
#   直接挂在宿主上,一关都没过——其中「启动任务派发 daemon」那条无需参数、无需凭证
#   就能进入进程启动路径。这里用一层 middleware 把同一份判断套到全部 /api/ 写接口
#   上(读接口不动),并在启动时自检有没有漏网的写路由。
#   这一段与 session_host_v1 的开关**无关**:旧接口在不在闸内,不该取决于新功能开没开。
# --------------------------------------------------------------------------- #
try:
    import session_bootstrap as _legacy_auth_bootstrap

    # flag 开着时复用会话 API 那份 policy(同一个对象,不会在两处配出两个答案);
    # 关着时这里自己建一份,并顺带把 `GET /api/session-auth/bootstrap` 挂上
    #   ——闸要 token,发 token 的那条路就不能跟着新功能的开关一起消失。
    _LEGACY_WRITE_AUTH = _legacy_auth_bootstrap.install_legacy_write_auth(
        app,
        policy=getattr(_SESSION_RUNTIME, "auth_policy", None),
        config_loader=_load_dashboard_config,
    )
    _UNPROTECTED_WRITES = _legacy_auth_bootstrap.unprotected_mutating_routes(app)
    if _UNPROTECTED_WRITES:
        print(
            "[legacy_write_auth] 以下写路由不在鉴权闸内,请把它们挪到 /api/ 下"
            "或显式加闸：" + "、".join(_UNPROTECTED_WRITES)
        )
except Exception as _legacy_auth_exc:  # noqa: BLE001
    _LEGACY_WRITE_AUTH = None
    print(f"[legacy_write_auth] 装配失败,旧写接口目前没有闸：{_legacy_auth_exc!r}")


_NO_CACHE = {"Cache-Control": "no-store"}   # index.html 绝不缓存(assets 带 hash 可长缓),否则发版后浏览器/WKWebView 吃旧壳


@app.get("/")
def index():
    if (WEB_DIST / "index.html").is_file():
        return FileResponse(WEB_DIST / "index.html", headers=_NO_CACHE)
    return RedirectResponse("http://localhost:5174/", status_code=307)


@app.get("/terminal-lab")
def terminal_lab_page():
    """SPA 子路径(main.tsx 按 pathname 分流)——同样回 index.html。"""
    if (WEB_DIST / "index.html").is_file():
        return FileResponse(WEB_DIST / "index.html", headers=_NO_CACHE)
    return RedirectResponse("http://localhost:5174/terminal-lab", status_code=307)


class _SpaStatic(StaticFiles):
    """SPA 深链回退：静态文件命中就返回,否则不含 `.` 的路径回 index.html。"""
    async def get_response(self, path, scope):
        from starlette.exceptions import HTTPException as _HttpError
        try:
            return await super().get_response(path, scope)
        except _HttpError as exc:  # 接口/WS/旧静态目录照旧 404,只兜前端路由
            if exc.status_code != 404 or path.startswith(("api/", "ws/", "static/")):
                raise
            if "." in path.rsplit("/", 1)[-1]:   # 带扩展名 = 缺资源,不是深链
                raise
            return await super().get_response("index.html", scope)


if WEB_DIST.is_dir():
    app.mount("/", _SpaStatic(directory=str(WEB_DIST), html=True), name="webdist")


if __name__ == "__main__":
    import uvicorn

    atexit.register(_shutdown_pty_runtimes_sync, "atexit")
    # config 实时同步器：后台守护线程，每 5s 把"实时继承"关系里父级的选定键推给子级
    threading.Thread(target=_config_sync_loop, args=(5,), daemon=True).start()
    # dashboard 实时事件流：监听组织树/Profile/技能/配置状态变化，SSE 推给前端自动刷新
    threading.Thread(target=_dashboard_event_watch_loop, args=(1.5,), daemon=True).start()
    print(f"Hermes 仪表板 → http://localhost:8877   (HERMES_HOME={HERMES_HOME})")
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8877,
            log_level="info",
            timeout_graceful_shutdown=2,
        )
    finally:
        _shutdown_pty_runtimes_sync("uvicorn-exit")
