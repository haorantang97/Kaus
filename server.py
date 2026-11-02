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
  - SOUL：直接读 SOUL.md。
  - 旧 PTY 终端（/ws/chat、/ws/terminal-lab、/api/pty/*）与 static/ 界面已于 2026-11-02 删除；
    对话走 session_bootstrap 挂载的 /api/conversations/*。

约定：所有路径用 os.path.expanduser / 绝对路径；HERMES_HOME 可用环境变量覆盖（便于测试）。
不 import hermes（无 Python SDK），只 subprocess + 直读文件。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import queue as queue_mod
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
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
import project_config_store
MODEL_OPTIONS_FILE = HERE / "model-options.json"
DASHBOARD_CONFIG_FILE = HERE / "dashboard-config.json"

# hermes profile 名规则（与 hermes_cli/profiles.py 一致），用于防注入
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")             # 技能名校验（防 argv 注入）
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SEP_CHARS = set("─-— ")  # 表格分隔行允许出现的字符（含 U+2500）


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
    if project_config_store.contains(HERE, name):
        return project_config_store.directory(HERE, name)
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
    return sorted(set(names) | set(project_config_store.names(HERE)))


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
    existing = {r["name"] for r in rows}
    rows.extend({"name": name, "is_default": False} for name in project_config_store.names(HERE) if name not in existing)
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
            "native_engine": None if project_config_store.contains(HERE, nm) else "hermes",
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
    "model-options.json",
)
_PROFILE_EVENT_FILES = (
    "config.yaml",
    ".dash_inherited.json",
    "SOUL.md",
    "config.yaml.dashbak",
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
    ignored_dirs = {".git", ".hub", ".archive", ".venv", "node_modules", "__pycache__"}
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
        if entry.name in ignored_dirs:
            continue
        # 顶层 entry 的 lstat 足以捕捉技能增删、symlink 迁移和 .usage.json 更新。
        out[entry.name] = _state_stat_token(entry)
        n += 1
        if entry.is_dir() and not entry.is_symlink():
            # Prune before traversing: filtering rglob results still walks every
            # dependency and archive directory on each dashboard refresh.
            for directory, dirs, files in os.walk(entry, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in ignored_dirs)
                if "SKILL.md" in files:
                    skill_file = Path(directory) / "SKILL.md"
                    out[str(skill_file.relative_to(root))] = _state_stat_token(skill_file)
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
            "scopes": ["network", "profiles", "skills", "config", "kanban"],
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
        elif name in {"skill-blocklist.json", "skill_inherit_off.json"}:
            scopes.update({"network", "profiles", "skills"})
        elif name in {"model-options.json", "config_inherit_block.json", "constitution_state.json"}:
            scopes.update({"profiles", "config"})

    if "root_config" in changed:
        scopes.update({"profiles", "config"})
    if "profiles" in changed:
        scopes.update({"network", "profiles", "skills", "config", "kanban"})
    if "profile_files" in changed:
        scopes.update({"network", "profiles", "config"})
    if "memories" in changed:
        scopes.update({"profiles", "memory"})
    if "skills" in changed:
        scopes.update({"network", "profiles", "skills"})

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


@app.get("/api/events")
def api_events():
    q: queue_mod.Queue = queue_mod.Queue(maxsize=64)
    with _EVENT_LOCK:
        _EVENT_SUBSCRIBERS.add(q)

    async def event_stream():
        try:
            snapshot = _EVENT_LAST_SIGNATURE
            if snapshot is None:
                snapshot = await asyncio.to_thread(_dashboard_state_signature)
            yield _sse_payload(_dashboard_event_from_signatures(None, snapshot))
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
# Dashboard 自身配置（dashboard-config.json）
# --------------------------------------------------------------------------- #
def _load_dashboard_config() -> dict:
    """读取 dashboard 自身配置（非 agent 配置）。"""
    try:
        return json.loads(DASHBOARD_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.get("/api/health")
def health():
    return {"ok": True, "hermes_bin": HERMES_BIN, "hermes_home": str(HERMES_HOME)}


@app.get("/api/network")
def api_network():
    """Agent 组织关系网（树）：parent/children/in_network/role/symlinks/skill_count。"""
    return build_tree()


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
    """创建项目配置草稿，不创建或连接任何 Agent。"""
    name = (req.name or "").strip()
    if not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名（连字符小写、长度 1-32）")
    if name == "default":
        raise HTTPException(409, "default 是主 agent，不可新建同名")
    if _profile_dir(name).is_dir():
        raise HTTPException(409, f"profile {name} 已存在")
    desc = (req.description or "").strip()
    if not HERMES_HOME.is_dir() and not project_config_store.contains(HERE, "default"):
        project_config_store.create(HERE, "default")
    project_config_store.create(HERE, name, description=desc)
    _invalidate_profile_list_cache()
    if req.display:
        _set_label(name, req.display)            # 写显示名映射
    # 加进草稿名单（hierarchy.json 不写入；effective_parent 自动返回 None）
    drafts = _load_drafts()
    drafts.add(name)
    _save_drafts(drafts)
    return {"ok": True, "name": name, "label": _label(name), "is_draft": True,
            "description": desc}


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
    """复制项目配置到同级项目；不复制登录、原生历史或记忆。"""
    if name != "default" and not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法源 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "源 profile 不存在")
    new = (req.new_name or "").strip()
    if not _PROFILE_ID_RE.match(new):
        raise HTTPException(400, "非法新 profile 名（小写连字符）")
    if new == "default" or _profile_dir(new).is_dir():
        raise HTTPException(409, f"目标名已存在: {new}")
    project_config_store.create(HERE, new, source=_profile_dir(name))
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


def _anchor_path(parent, h: dict) -> list:
    """root→parent 的祖先路径（含 parent 本身）。parent 为空 → []。"""
    if not parent:
        return []
    return list(reversed([parent, *_ancestor_chain(parent, h)]))


def _project_subtree(name: str, h: dict) -> list:
    """name 的全部下级，按组织树（effective_parent）由近到远（BFS 顺序），不含 name、不含 default。"""
    by_parent: dict = {}
    for nm in _all_profile_names():
        if nm in (name, MAIN_AGENT):
            continue
        p = effective_parent(nm, h)
        if p and p != nm:
            by_parent.setdefault(p, []).append(nm)
    out, seen, queue = [], {name}, [name]
    while queue:
        for child in sorted(by_parent.get(queue.pop(0), [])):
            if child not in seen:
                seen.add(child)
                out.append(child)
                queue.append(child)
    return out


def _project_delete_order(name: str) -> list:
    """级联删除顺序：最深的下级先删，name 最后。供 project_config_sync 统计整棵子树的运行对话。"""
    if name == MAIN_AGENT or not _PROFILE_ID_RE.match(name or ""):
        return [name]
    return [*reversed(_project_subtree(name, _load_hierarchy())), name]


@app.get("/api/agent/{name}/delete-preview")
def api_delete_preview(name: str):
    """删除确认用：将一并删除的下级（由上到下）。"""
    if name == "default" or not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    labels = _load_labels()
    return {"name": name, "descendants": [
        {"name": nm, "label": _label(nm, labels)} for nm in _project_subtree(name, _load_hierarchy())]}


def _delete_one_project(name: str, record: dict) -> None:
    """删除单个 Project 并清掉 dashboard 状态引用。Kaus 配置连同 record 一起归档；Hermes profile 走 CLI 删除。"""
    h = _load_hierarchy()
    if project_config_store.contains(HERE, name):
        project_config_store.archive(HERE, name, record=record)
    else:
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


@app.delete("/api/agent/{name}")
def api_delete_agent(name: str):
    """删除一个 agent 及其整棵子树（最深的先删）。default 不可删。
       每个 Project 各自归档一份删除记录（树位置、root→parent 祖先路径、同一 batchId 与 batchRoot），可原位恢复；
       Hermes profile 走 hermes profile delete（归档到 trash）。
       有对话运行中时由 project_config_sync 先行拒绝（409，统计整棵子树；带 ?force=1 先停止再删）；
       删除后对话归档保留，不删除。"""
    if name == "default":
        raise HTTPException(400, "default 是主 agent，不可删除")
    if not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if not _profile_dir(name).is_dir():
        raise HTTPException(404, "profile 不存在")
    h = _load_hierarchy()
    order = [*reversed(_project_subtree(name, h)), name]
    for nm in order:
        if nm == MAIN_AGENT or not _PROFILE_ID_RE.match(nm):
            raise HTTPException(400, f"非法 profile 名: {nm}")
    # 记录全部在删除前按同一棵树算好，避免先删的下级改变上级的路径。
    import uuid
    batch_id = uuid.uuid4().hex
    labels, drafts, pinned, killed = _load_labels(), _load_drafts(), _load_pinned(), _load_killed()
    block_all, skill_off = _load_config_block_all(), _load_skill_inherit_off()
    records = {nm: {
        "inHierarchy": nm in h, "parent": h.get(nm),
        "ancestors": _anchor_path(effective_parent(nm, h), h),
        "batchId": batch_id, "batchRoot": name,
        "label": labels.get(nm), "draft": nm in drafts,
        "pinned": nm in pinned, "killed": nm in killed,
        "configBlock": block_all.get(nm), "skillInheritOff": nm in skill_off,
    } for nm in order}
    deleted: list = []
    for nm in order:
        try:
            _delete_one_project(nm, records[nm])
        except HTTPException as exc:
            if not deleted:
                raise
            raise HTTPException(exc.status_code, f"{exc.detail}（{nm} 未删除；已删除: {', '.join(deleted)}）")
        deleted.append(nm)
    return {"ok": True, "deleted": name, "cascade": deleted[:-1], "batchId": batch_id}


class RestoreDeletedReq(BaseModel):
    # 仅当归档里没有位置记录（本功能之前删除的项目）时使用：领域库保留的原父级。
    parent: str | None = None


def _restore_refused(status: int, code: str, message: str, **fields):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": message, "error": {"code": code, "message": message, **fields}}, status_code=status)


def _restore_one_project(name: str, archived, record: dict, fallback_parent: str | None = None):
    """按 record 把一份归档原位恢复。返回 (parent, None) 或 (None, (code, message))，失败时不改动任何东西。"""
    if _profile_dir(name).is_dir():
        return None, ("project_exists", "同名 Project 已存在。")
    if record:
        in_hierarchy, parent = bool(record.get("inHierarchy")), record.get("parent") or None
    elif fallback_parent:
        in_hierarchy, parent = True, fallback_parent.removeprefix("project:")
    else:
        return None, ("project_parent_missing", "原上级 Project 未知。")
    if parent is not None and (
        (parent != "default" and not _PROFILE_ID_RE.match(parent)) or not _profile_dir(parent).is_dir()
    ):
        return None, ("project_parent_missing", "原上级 Project 已不存在。")
    # 锚点：原上级还在，但它到根的路径变了（被移动过）→ 不原位恢复。较早的删除记录没有路径，只看上级是否存在。
    recorded = record.get("ancestors") if record else None
    if parent is not None and isinstance(recorded, list) and _anchor_path(parent, _load_hierarchy()) != recorded:
        return None, ("project_anchor_moved", "原位置已变动。")
    project_config_store.restore(HERE, name, archived)
    _invalidate_profile_list_cache()
    if in_hierarchy:
        h = _load_hierarchy(); h[name] = parent; _save_hierarchy(h)
    if record.get("label"):
        _set_label(name, record["label"])
    if record.get("draft"):
        drafts = _load_drafts(); drafts.add(name); _save_drafts(drafts)
    if record.get("pinned"):
        pinned = _load_pinned()
        if name not in pinned:
            pinned.append(name); _save_pinned(pinned)
    if record.get("killed"):
        ks = _load_killed(); ks.add(name); _save_killed(ks)
    if isinstance(record.get("configBlock"), list):
        block = _load_config_block_all(); block[name] = record["configBlock"]; _save_config_block_all(block)
    if record.get("skillInheritOff"):
        off = _load_skill_inherit_off(); off.add(name); _save_skill_inherit_off(off)
    return parent, None


@app.post("/api/agent/{name}/restore-deleted")
def api_restore_deleted_agent(name: str, req: RestoreDeletedReq | None = None):
    """把已删除的 Kaus 项目配置原位恢复（最新一份归档）。不恢复任何对话——由用户逐条恢复。
       级联删除的一员：同一批次里仍处于删除状态的上级链先由上到下恢复（不恢复兄弟）。
       每一步都按锚点规则检查；任一步失败即停止，返回该步的 code（已恢复的列在 restored 里）。
       原父级不在、路径已变、或没有可恢复的配置副本（如 Hermes profile 删除）时返回明确的 code，不猜位置。"""
    if name == "default" or not _PROFILE_ID_RE.match(name):
        raise HTTPException(400, "非法 profile 名")
    if _profile_dir(name).is_dir():
        return _restore_refused(409, "project_exists", "同名 Project 已存在。")
    archived = project_config_store.latest_archive(HERE, name)
    if archived is None:
        return _restore_refused(409, "project_archive_missing", "没有可恢复的 Project 配置。")
    record = project_config_store.deleted_record(archived)
    chain = [(name, archived, record)]
    batch, ancestors = record.get("batchId"), record.get("ancestors")
    if batch and isinstance(ancestors, list):
        for ancestor in reversed(ancestors):
            if (ancestor != "default" and not _PROFILE_ID_RE.match(str(ancestor))) or _profile_dir(ancestor).is_dir():
                break
            copy_ = project_config_store.latest_archive(HERE, ancestor)
            copy_record = project_config_store.deleted_record(copy_) if copy_ is not None else {}
            if copy_record.get("batchId") != batch:
                break
            chain.insert(0, (ancestor, copy_, copy_record))
    restored: list = []
    parent = None
    for nm, copy_, copy_record in chain:
        parent, failure = _restore_one_project(nm, copy_, copy_record,
                                               req.parent if req is not None and nm == name else None)
        if failure is not None:
            if restored:
                _materialize_tree(dry_run=False)
            return _restore_refused(409, *failure, project=nm, restored=restored)
        restored.append(nm)
    _materialize_tree(dry_run=False)
    return {"ok": True, "restored": name, "label": _label(name), "parent": parent, "chain": restored[:-1]}


# --------------------------------------------------------------------------- #
# 模型目录：组织树与软继承读取的模型条目（model 只读父链默认，不物化进子 config）。
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
    if entry.get("provider") and re.sub(r"[-_\s]+", "-", str(entry["provider"]).lower()) != re.sub(r"[-_\s]+", "-", str(known.get("provider") or "").lower()):
        return entry
    if entry.get("provider"):
        known["provider"] = entry["provider"]
    if entry.get("base_url"):
        known["base_url"] = entry["base_url"]
    return known


def _effective_model_entry(name: str, cache: dict | None = None) -> dict | None:
    return _canonical_model_entry(_soft_effective_model(name, cache))


async def _profile_model_choice(name: str, value: dict | str) -> dict:
    """Legacy profile edits use the same scoped catalog and selector as chat."""
    entry = _model_entry(value)
    if not entry:
        raise HTTPException(400, "非法模型配置")
    runtime = _SESSION_RUNTIME
    if runtime is None:
        raise HTTPException(503, "模型目录暂不可用，请稍后重试")
    from app.projects.binding_snapshot import register_binding_snapshot
    from drivers.model_selection import select_model
    from drivers.hermes.model_catalog import normalize_provider
    bindings = await runtime.repositories.bindings.list_for_project(f"project:{name}")
    binding = next((item for item in bindings if item.native_scope_ref == name
                    and item.backend_id == "backend:hermes"), None)
    if binding is None:
        raise HTTPException(400, "未找到此 Agent 的引擎配置")
    try:
        driver = runtime.registry.get(binding.backend_id)
        await register_binding_snapshot(driver, binding, runtime.repositories.projects)
        catalog = await driver.get_model_catalog(binding)
        selected = select_model(catalog, entry["default"], entry.get("provider"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "模型目录暂不可用，请稍后重试") from exc
    known = _known_model(selected.model_id) or {}
    # A catalog entry with the same model name may route to another vendor.
    # Never reuse that entry's endpoint, reasoning defaults or service tier.
    if normalize_provider(known.get("provider")) != normalize_provider(selected.provider_id):
        known = {}
    return {**known, "default": selected.model_id, "provider": selected.provider_id}


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
    if project_config_store.contains(HERE, name):
        return None  # Kaus projects inherit through the domain resolver, not native profile files.
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
    if project_config_store.contains(HERE, req.assignee):
        raise HTTPException(409, "任务板仅支持 Hermes 原生实例")
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
        if project_config_store.contains(HERE, who):
            raise HTTPException(409, "任务板仅支持 Hermes 原生实例")
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


# ── Dashboard 自身配置 API ──────────────────────────────────────────


# ---- per-agent 技能管理：成员制（自有/继承 分类 + 装 install / 卸 uninstall / 解除 继承） ----


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


if _SESSION_RUNTIME is not None:
    import project_config_sync
    project_config_sync.install(app, sys.modules[__name__], _SESSION_RUNTIME.repositories,
                                session_host=_SESSION_RUNTIME.session_host,
                                surface_coordinator=getattr(_SESSION_RUNTIME, "surface_coordinator", None))


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

    # config 实时同步器：后台守护线程，每 5s 把"实时继承"关系里父级的选定键推给子级
    threading.Thread(target=_config_sync_loop, args=(5,), daemon=True).start()
    # dashboard 实时事件流：监听组织树/Profile/技能/配置状态变化，SSE 推给前端自动刷新
    threading.Thread(target=_dashboard_event_watch_loop, args=(1.5,), daemon=True).start()
    print(f"Hermes 仪表板 → http://localhost:8877   (HERMES_HOME={HERMES_HOME})")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8877,
        log_level="info",
        timeout_graceful_shutdown=2,
    )
