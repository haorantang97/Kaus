#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_inheritable_secrets.py — 继承键敏感值审计（R-06 / 基线 §5.5 / §19 Q-17）

盘点 `_INHERITABLE_KEYS` 这 16 个继承键在 `~/.hermes/config.yaml` 与每个
`~/.hermes/profiles/*/config.yaml` 里的分布，回答：

  * 每个继承键在多少个 profile 中出现；
  * 其中多少个含**疑似明文凭据**；
  * 其中多少个只是 `${ENV}` **引用**（引用而非明文，单独归类）；
  * 这些键是否由仪表盘 `_materialize_config` 物化写入（读 `.dash_inherited.json`）。

**绝对纪律（基线 §5.5）：只输出键路径与计数，绝不输出任何值。**
脚本在打印前会做一次自检：把**被判定为敏感的值**（明文嫌疑值，以及引用值去掉
`${...}` 占位符后的实体残余）与最终输出对撞，一旦有值泄漏进输出就直接抛错退出，
而不是打印出来；报错只说分类与长度，不说值。
良性值（benign / path_ref / 纯数字 / 短于 12 字符 / 等于某个 profile 名或键路径分段，
例如 `default`、`medium`、`deepseek`）**不纳入**守卫 —— 它们本来就会合法地出现在
报告里（profile 名、键路径、章节文字），拿它们当泄漏证据是纯误报。

只读取两类文件：
  * <profile>/config.yaml
  * <profile>/.dash_inherited.json
**从不**读取 .env / credentials/ / auth* / 任何别的东西。

依赖：Python 3.9+ 标准库。PyYAML 存在时用它解析（更准）；不存在时用内置的
最小 YAML 子集解析器（够解析 `yaml.safe_dump` 产出的 config.yaml）。
所以在 macOS 上 `/usr/bin/python3` 直接能跑，也可以用
`/opt/homebrew/bin/python3.11` 跑得更准。

用法：
    python3 scripts/ops/audit_inheritable_secrets.py                  # markdown 到 stdout
    python3 scripts/ops/audit_inheritable_secrets.py --json           # JSON 到 stdout
    python3 scripts/ops/audit_inheritable_secrets.py --json-out a.json # markdown + 落 JSON 文件
    python3 scripts/ops/audit_inheritable_secrets.py --self-test      # 内置 fixture 自检
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

# =========================================================================== #
# 1. 16 个继承键 —— 从 server.py 的 _INHERITABLE_KEYS 原样抄，顺序不动。
#    改动 server.py 时必须同步这里（README 有校验命令）。
# =========================================================================== #
INHERITABLE_KEYS = [
    "providers", "fallback_providers", "credential_pool_strategies",
    "mcp_servers", "toolsets", "agent", "tool_loop_guardrails", "compression",
    "context", "prompt_caching", "auxiliary", "image_gen", "memory", "delegation", "curator", "hooks",
]
assert len(INHERITABLE_KEYS) == 16, "继承键必须是 16 个（与 server.py 一致）"

INHERIT_TRACK = ".dash_inherited.json"      # server.py: _INHERIT_TRACK
MAIN_AGENT = "default"                      # server.py: MAIN_AGENT

# =========================================================================== #
# 2. 凭据形状识别规则（可审计清单）
# =========================================================================== #

# 2a. 已知凭据前缀 —— (正则, 标签)
CRED_PREFIX_RULES = [
    (re.compile(r"^sk-ant-"),            "prefix:sk-ant-"),
    (re.compile(r"^sk-proj-"),           "prefix:sk-proj-"),
    (re.compile(r"^sk-or-"),             "prefix:sk-or-"),
    (re.compile(r"^sk-[A-Za-z0-9]"),     "prefix:sk-"),
    (re.compile(r"^gh[pousr]_"),         "prefix:ghp_/gho_/ghu_/ghs_/ghr_"),
    (re.compile(r"^github_pat_"),        "prefix:github_pat_"),
    (re.compile(r"^glpat-"),             "prefix:glpat-"),
    (re.compile(r"^xox[abprse]-"),       "prefix:xox*-"),
    (re.compile(r"^xapp-"),              "prefix:xapp-"),
    (re.compile(r"^A[KS]IA[0-9A-Z]{8,}"), "prefix:AKIA/ASIA"),
    (re.compile(r"^AIza[0-9A-Za-z_\-]{10,}"), "prefix:AIza"),
    (re.compile(r"^ya29\."),             "prefix:ya29."),
    (re.compile(r"^hf_[A-Za-z0-9]{10,}"), "prefix:hf_"),
    (re.compile(r"^gsk_[A-Za-z0-9]{10,}"), "prefix:gsk_"),
    (re.compile(r"^dop_v1_"),            "prefix:dop_v1_"),
    (re.compile(r"^shp(at|ss|ca|pa)_"),  "prefix:shpat_"),
    (re.compile(r"^SG\.[A-Za-z0-9_\-]{10,}"), "prefix:SG."),
    (re.compile(r"^npm_[A-Za-z0-9]{10,}"), "prefix:npm_"),
    (re.compile(r"^pypi-[A-Za-z0-9_\-]{10,}"), "prefix:pypi-"),
    (re.compile(r"^r8_[A-Za-z0-9]{10,}"), "prefix:r8_"),
    (re.compile(r"^Bearer\s+\S{8,}"),    "prefix:Bearer "),
    (re.compile(r"^-----BEGIN [A-Z ]*PRIVATE KEY"), "pem-private-key"),
]

# 2b. 名字像凭据的子键（大小写不敏感，按 . _ - 分词后匹配整词或整名）
SUSPICIOUS_KEY_RE = re.compile(
    r"(?:^|[._\-])"
    r"(?:api[_\-]?key|apikey|key|keys|secret|secrets|token|tokens|password|passwd|pass|"
    r"credential|credentials|cred|auth|authorization|bearer|"
    r"access[_\-]?key|private[_\-]?key|client[_\-]?secret|session[_\-]?key|signing[_\-]?key)"
    r"(?:$|[._\-])",
    re.IGNORECASE,
)

# 2c. 环境变量 / 外部引用（= 引用而非明文，单独归类）
ENV_REF_RES = [
    re.compile(r"\$\{[^}]+\}"),               # ${OPENAI_API_KEY}
    re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$"),  # $OPENAI_API_KEY
    re.compile(r"^!?env:", re.IGNORECASE),    # env:NAME / !env:NAME
    re.compile(r"^%[A-Za-z_][A-Za-z0-9_]*%$"),  # %NAME%
    re.compile(r"^\{\{\s*[A-Za-z_][A-Za-z0-9_.]*\s*\}\}$"),  # {{ NAME }}
    re.compile(r"^(?:secret|credential)_?ref:", re.IGNORECASE),
]

# 2d. 明显不是凭据的形状
PATH_REF_RE = re.compile(r"^(?:~|\.{1,2}/|/)[^\s]*$")
URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
SLUG_RE = re.compile(r"^[a-z0-9]+(?:[._\-/][a-z0-9]+)*$")   # 模型 id / 主机名 / 工具名
HEXLIKE_RE = re.compile(r"^[0-9a-fA-F]{32,}$")
NUMERIC_RE = re.compile(r"^[-+]?[0-9._eE+\-]+$")

MIN_RANDOM_LEN = 20         # 低于这个长度不做熵判定
MIN_KEYNAME_VALUE_LEN = 8   # 名字可疑 + 值至少这么长才算明文嫌疑

# 泄漏守卫只盯"真正敏感"的值。短于这个长度的值不纳入 —— 良性枚举/名字
# （`default` / `medium` / `deepseek` / `high` …）会合法地出现在输出里
# （profile 名、键路径分段、章节文字），拿它们当泄漏证据是纯误报。
MIN_GUARD_LEN = 12

# `${VAR}` / `$VAR` / `%VAR%` / `{{ VAR }}` 这些占位符本身不是秘密，
# 守卫只看引用值里除占位符以外的残余部分。
_PLACEHOLDER_RE = re.compile(
    r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%|\{\{[^}]*\}\}")


def reference_residue(s: str) -> str:
    """引用值去掉占位符后剩下的实体文本（通常是空的）。"""
    return _PLACEHOLDER_RE.sub(" ", s).strip()


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = float(len(s))
    return -sum((c / n) * math.log(c / n, 2) for c in counts.values())


def looks_random(s: str) -> bool:
    """长随机串判定：够长、无空白、字符集混杂、熵够高，且不像模型 id / 主机名 / 路径 / URL。"""
    if len(s) < MIN_RANDOM_LEN:
        return False
    if any(ch.isspace() for ch in s):
        return False
    if URL_RE.match(s) or PATH_REF_RE.match(s) or NUMERIC_RE.match(s):
        return False
    if SLUG_RE.match(s):
        return False
    if HEXLIKE_RE.match(s):
        return True
    classes = sum([
        any(c.islower() for c in s),
        any(c.isupper() for c in s),
        any(c.isdigit() for c in s),
        any(c in "+/=_-~." for c in s),
    ])
    return classes >= 3 and shannon_entropy(s) >= 3.2


def is_env_ref(s: str) -> bool:
    return any(rx.search(s) for rx in ENV_REF_RES)


def cred_prefix_label(s: str):
    for rx, label in CRED_PREFIX_RULES:
        if rx.match(s):
            return label
    return None


def leaf_key_name(path) -> str:
    """叶子键名：跳过列表下标（`[3]`），取最近的一个真实键名。"""
    for seg in reversed(path):
        s = str(seg)
        if not re.fullmatch(r"\[\d+\]", s):
            return s
    return ""


def key_name_suspicious(path) -> bool:
    """只看**叶子键名**。祖先键名（例如 `credential_pool_strategies`）不算，
       否则整棵子树都会被误判成凭据。"""
    return bool(SUSPICIOUS_KEY_RE.search(leaf_key_name(path)))


#: 分类结果标签
CLS_PLAINTEXT = "plaintext_suspect"
CLS_REFERENCE = "reference"
CLS_PATH = "path_ref"
CLS_BENIGN = "benign"
CLS_EMPTY = "empty"


def classify(path, value: str):
    """返回 (classification, reason)。只看形状，不记录、不返回值本身。"""
    if value is None:
        return CLS_EMPTY, "null"
    s = str(value)
    if s.strip() == "" or s.strip() in ("{}", "[]", "null", "~", "none", "None"):
        return CLS_EMPTY, "empty"
    if is_env_ref(s):
        return CLS_REFERENCE, "env-or-secret-ref"
    if NUMERIC_RE.match(s):
        # 纯数字/版本号永远不是凭据 —— 挡住 max_tokens / trigger_tokens 这类误报
        return CLS_BENIGN, "numeric"
    label = cred_prefix_label(s)
    if label:
        return CLS_PLAINTEXT, label
    if PATH_REF_RE.match(s):
        return CLS_PATH, "filesystem-path"
    if key_name_suspicious(path):
        if len(s) >= MIN_KEYNAME_VALUE_LEN and not any(c.isspace() for c in s) and not URL_RE.match(s):
            return CLS_PLAINTEXT, "suspicious-key-name"
        return CLS_BENIGN, "suspicious-key-name-but-value-shape-benign"
    if looks_random(s):
        return CLS_PLAINTEXT, "high-entropy-token"
    return CLS_BENIGN, "no-credential-shape"


def redact_segment(seg: str) -> str:
    """路径分段本身像凭据时（例如有人把 key 当成 mapping 键），换成占位符。"""
    s = str(seg)
    if cred_prefix_label(s) or looks_random(s):
        return "<redacted-segment>"
    return s


def path_str(path) -> str:
    return ".".join(redact_segment(p) for p in path)


def normalize_path(path):
    """把列表下标折叠成 []，用于跨 profile 聚合。"""
    return tuple("[]" if isinstance(p, str) and re.fullmatch(r"\[\d+\]", p) else p for p in path)


# =========================================================================== #
# 3. YAML 读取：优先 PyYAML，否则内置最小子集解析器
# =========================================================================== #

try:  # pragma: no cover - 取决于运行环境
    import yaml as _pyyaml
except Exception:  # pragma: no cover
    _pyyaml = None

#: 'auto' = 有 PyYAML 就用；'builtin' = 强制用内置最小解析器（用于对拍 / macOS 复现）
FORCE_PARSER = "auto"


def _scalar_to_text(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def flatten_obj(obj, prefix=()):
    """把已解析的对象拍平成 (leaves, containers)。leaves 的值一律转成文本。"""
    leaves, containers = {}, set()
    if isinstance(obj, dict):
        containers.add(prefix)
        for k, v in obj.items():
            sub_l, sub_c = flatten_obj(v, prefix + (str(k),))
            leaves.update(sub_l)
            containers |= sub_c
    elif isinstance(obj, (list, tuple)):
        containers.add(prefix)
        for i, v in enumerate(obj):
            sub_l, sub_c = flatten_obj(v, prefix + ("[%d]" % i,))
            leaves.update(sub_l)
            containers |= sub_c
    else:
        if prefix:
            leaves[prefix] = _scalar_to_text(obj)
    return leaves, containers


# --- 内置最小 YAML 解析（只覆盖 yaml.safe_dump 产出的形状 + 常见手写形状） ---

_KEY_LINE_RE = re.compile(
    r"^(?P<key>\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'|[^:#\-\s][^:#]*?|-)\s*:(?:\s+(?P<val>.*?))?\s*$"
)


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == s[-1] == '"':
        body = s[1:-1]
        return (body.replace('\\"', '"').replace("\\n", "\n")
                    .replace("\\t", "\t").replace("\\\\", "\\"))
    return s


def _quote_closed(s: str) -> bool:
    """s 以引号开头时，判断这一段里引号是否已经闭合（用于识别 PyYAML 的折行标量）。"""
    if not s:
        return True
    q = s[0]
    if q == "'":
        i = 1
        while i < len(s):
            if s[i] == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    i += 2
                    continue
                return True
            i += 1
        return False
    if q == '"':
        i = 1
        while i < len(s):
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == '"':
                return True
            i += 1
        return False
    return True


def _strip_inline_comment(s: str) -> str:
    s = s.strip()
    if s[:1] in ("'", '"'):
        return s                      # 引号里可能有 #，不动
    idx = s.find(" #")
    return s[:idx].strip() if idx >= 0 else s


def flatten_yaml_text(text: str):
    """(leaves, containers, errors) —— 行扫描式的最小 YAML 解析。

    支持：块映射、块序列（`-` 与父键同缩进或更深）、序列里的映射、单/双引号标量、
    块标量 `|` `>`、注释、空值、`{}` / `[]`。不支持锚点/别名/复杂 flow —— 遇到就记 error。
    """
    leaves, containers, errors = {}, set(), []
    containers.add(())
    stack = []          # [(indent, key, seq_counter)]
    root_seq = {}       # 顶层序列的计数器（config.yaml 顶层应当是 mapping，这里只是兜底）
    lines = text.splitlines()
    i = 0
    n = len(lines)

    def cur_path():
        return tuple(fr[1] for fr in stack)

    while i < n:
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.lstrip().startswith("---") or raw.lstrip().startswith("..."):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            errors.append("第 %d 行缩进含 TAB，YAML 不允许" % i)
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()

        if content.startswith("&") or content.startswith("*"):
            errors.append("第 %d 行使用了 YAML 锚点/别名，内置解析器不支持" % i)
            continue

        # ---- 序列项 ----
        if content == "-" or content.startswith("- "):
            while stack and stack[-1][0] > indent:
                stack.pop()
            if stack and stack[-1][0] == indent and stack[-1][1].startswith("["):
                stack.pop()
            parent = cur_path()
            containers.add(parent)
            # 找同层的计数器：挂在父 frame 上
            if stack:
                stack[-1][2].setdefault(indent, 0)
                idx = stack[-1][2][indent]
                stack[-1][2][indent] = idx + 1
            else:
                root_seq.setdefault(indent, 0)
                idx = root_seq[indent]
                root_seq[indent] = idx + 1
            item_key = "[%d]" % idx
            rest = content[1:].strip()
            if not rest:
                stack.append((indent, item_key, {}))
                continue
            # `- key: value`：把 dash 换成空格后按映射行重新处理
            content_col = len(raw) - len(raw.lstrip(" "))
            after = raw[content_col + 1:]
            extra = len(after) - len(after.lstrip(" "))
            inner_indent = content_col + 1 + extra
            if _KEY_LINE_RE.match(rest):
                stack.append((indent, item_key, {}))
                raw = " " * inner_indent + rest
                indent = inner_indent
                content = rest
                # 落到下面的映射分支
            else:
                leaves[parent + (item_key,)] = _unquote(_strip_inline_comment(rest))
                continue

        # ---- 映射行 ----
        m = _KEY_LINE_RE.match(content)
        if not m:
            # PyYAML 会把没有引号的长标量在空格处折行；续行并回上一个 leaf。
            # （凭据不含空格，永远不会被折行，所以这条兜底不会影响判定。）
            if leaves:
                last = list(leaves)[-1]     # dict 保序（3.7+）
                leaves[last] = (leaves[last] + " " + _unquote(content)).strip()
                continue
            errors.append("第 %d 行无法解析（内置解析器）" % i)
            continue

        key = _unquote(m.group("key"))
        val = m.group("val")

        # 映射键出现在缩进 I：任何缩进 >= I 的 frame（映射键 frame 与序列项 frame 都算）
        # 都已经结束。属于序列项的内联映射键缩进严格大于 `-` 的缩进，所以不会被误弹。
        while stack and stack[-1][0] >= indent:
            stack.pop()

        path = cur_path() + (key,)

        if val is None or val.strip() == "":
            containers.add(path)
            stack.append((indent, key, {}))
            continue

        v = val.strip()
        if v in ("|", "|-", "|+", ">", ">-", ">+"):
            buf = []
            while i < n:
                nxt = lines[i]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip(" "))) <= indent:
                    break
                buf.append(nxt.strip())
                i += 1
            leaves[path] = "\n".join(buf).strip()
            continue
        if v in ("{}", "[]"):
            containers.add(path)          # 空容器：与 PyYAML 一致，不产生叶子
            continue
        if v.startswith("{") or v.startswith("["):
            containers.add(path)
            leaves[path] = _strip_inline_comment(v)   # flow 集合按整串做形状判定
            continue
        if v[:1] in ("'", '"') and not _quote_closed(v):
            # PyYAML 会把长标量在空格处折行；把续行并回来再去引号
            parts = [v]
            while i < n and not _quote_closed(" ".join(parts)):
                parts.append(lines[i].strip())
                i += 1
            v = " ".join(parts)
        leaves[path] = _unquote(_strip_inline_comment(v))

    return leaves, containers, errors


def load_yaml_flat(path: Path):
    """返回 (leaves, containers, parser_name, errors)。读不到就报 error，绝不静默当空。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, set(), "none", ["读取失败: %s" % exc.__class__.__name__]
    if _pyyaml is not None and FORCE_PARSER != "builtin":
        try:
            data = _pyyaml.safe_load(text)
            if data is None:
                data = {}
            if not isinstance(data, dict):
                return {}, set(), "pyyaml", ["顶层不是 mapping"]
            leaves, containers = flatten_obj(data)
            return leaves, containers, "pyyaml", []
        except Exception as exc:  # YAMLError 等
            return {}, set(), "pyyaml", ["PyYAML 解析失败: %s" % exc.__class__.__name__]
    leaves, containers, errors = flatten_yaml_text(text)
    return leaves, containers, "builtin", errors


def load_json_flat(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, set(), ["读取失败: %s" % exc.__class__.__name__]
    try:
        data = json.loads(text)
    except ValueError:
        return {}, set(), ["JSON 解析失败"]
    if not isinstance(data, dict):
        return {}, set(), ["顶层不是 object"]
    leaves, containers = flatten_obj(data)
    return leaves, containers, []


# =========================================================================== #
# 4. 扫描
# =========================================================================== #

def discover_profiles(hermes_home: Path):
    """[(name, dir)] —— default = HERMES_HOME 本体，其余 = profiles/<name>（与 server.py 一致）。"""
    out = []
    if hermes_home.is_dir():
        out.append((MAIN_AGENT, hermes_home))
    pdir = hermes_home / "profiles"
    if pdir.is_dir():
        for entry in sorted(pdir.iterdir()):
            try:
                if entry.is_dir():
                    out.append((entry.name, entry))
            except OSError:
                continue
    return out


def subtree(flat: dict, key: str):
    return {p: v for p, v in flat.items() if p and p[0] == key}


def audit(hermes_home: Path, only_profiles=None):
    profiles = discover_profiles(hermes_home)
    if only_profiles:
        wanted = set(only_profiles)
        profiles = [(n, d) for n, d in profiles if n in wanted]

    files_read = []
    parse_problems = []
    per_profile = {}
    parsers_used = set()

    # 泄漏守卫的原料，只在内存里：
    #   guard_raw      —— [(值, 分类)]，稍后按分类/长度/白名单筛出真正敏感的
    #   exclusion_terms —— profile 名与所有键路径分段。这些**本来就会**合法出现在
    #                     报告里，值若正好等于其中之一（例如 `default`）不算泄漏。
    guard_raw = []
    exclusion_terms = set(INHERITABLE_KEYS)
    exclusion_terms.update([MAIN_AGENT, INHERIT_TRACK, "config.yaml",
                            CLS_PLAINTEXT, CLS_REFERENCE, CLS_PATH, CLS_BENIGN, CLS_EMPTY])

    for name, pdir in profiles:
        cfg_path = pdir / "config.yaml"
        trk_path = pdir / INHERIT_TRACK

        rec = {
            "profile": name,
            "dir": str(pdir),
            "config_yaml_present": cfg_path.exists(),
            "config_yaml_is_symlink": cfg_path.is_symlink(),
            "track_present": trk_path.exists(),
            "keys_present": [],
            "keys_in_track": [],
            "keys_track_matches_config": [],
            "keys_track_differs_from_config": [],
            "findings": [],          # [{path, normalized, classification, reason, in_track}]
            "parser": None,
        }

        cfg_leaves, cfg_containers = {}, set()
        if cfg_path.exists():
            files_read.append(str(cfg_path))
            cfg_leaves, cfg_containers, parser, errors = load_yaml_flat(cfg_path)
            rec["parser"] = parser
            parsers_used.add(parser)
            for e in errors:
                parse_problems.append({"profile": name, "file": "config.yaml", "problem": e})

        trk_leaves = {}
        if trk_path.exists():
            files_read.append(str(trk_path))
            trk_leaves, _tc, terrors = load_json_flat(trk_path)
            for e in terrors:
                parse_problems.append({"profile": name, "file": INHERIT_TRACK, "problem": e})

        exclusion_terms.add(name)
        for p in list(cfg_leaves) + list(cfg_containers) + list(trk_leaves):
            exclusion_terms.update(str(seg) for seg in p)
        for src in (cfg_leaves, trk_leaves):
            for p, v in src.items():
                if isinstance(v, str):
                    guard_raw.append((v, classify(p, v)[0]))

        present_paths = set(cfg_containers) | set(cfg_leaves)
        for key in INHERITABLE_KEYS:
            has_key = any(p and p[0] == key for p in present_paths)
            if has_key:
                rec["keys_present"].append(key)

            in_track = any(p and p[0] == key for p in trk_leaves)
            if in_track:
                rec["keys_in_track"].append(key)
                cfg_sub = subtree(cfg_leaves, key)
                trk_sub = subtree(trk_leaves, key)
                if cfg_sub == trk_sub:
                    rec["keys_track_matches_config"].append(key)
                else:
                    rec["keys_track_differs_from_config"].append(key)

            if not has_key:
                continue
            for p, v in sorted(subtree(cfg_leaves, key).items()):
                cls, reason = classify(p, v)
                # 只收「命中凭据形状 / 是引用 / 叶子键名可疑」的条目，其余不进报告。
                if cls not in (CLS_PLAINTEXT, CLS_REFERENCE) and not key_name_suspicious(p):
                    continue
                rec["findings"].append({
                    "key": key,
                    "path": path_str(p),
                    "normalized": path_str(normalize_path(p)),
                    "classification": cls,
                    "reason": reason,
                    "suspicious_key_name": key_name_suspicious(p),
                    "in_dash_inherited": p in trk_leaves,
                })

        # .dash_inherited.json 自身是否也留了一份明文副本
        rec["track_plaintext_keys"] = sorted({
            p[0] for p, v in trk_leaves.items()
            if p and p[0] in INHERITABLE_KEYS and classify(p, v)[0] == CLS_PLAINTEXT
        })

        per_profile[name] = rec

    # ---- 按继承键聚合 ----
    by_key = {}
    for key in INHERITABLE_KEYS:
        present, plaintext, reference, pathref, suspicious = [], [], [], [], []
        materialized, mat_unchanged, mat_changed, track_copy = [], [], [], []
        for name, rec in per_profile.items():
            if key in rec["keys_present"]:
                present.append(name)
            kf = [f for f in rec["findings"] if f["key"] == key]
            if any(f["classification"] == CLS_PLAINTEXT for f in kf):
                plaintext.append(name)
            if any(f["classification"] == CLS_REFERENCE for f in kf):
                reference.append(name)
            if any(f["classification"] == CLS_PATH for f in kf):
                pathref.append(name)
            if any(f["suspicious_key_name"] for f in kf):
                suspicious.append(name)
            if key in rec["keys_in_track"]:
                materialized.append(name)
            if key in rec["keys_track_matches_config"]:
                mat_unchanged.append(name)
            if key in rec["keys_track_differs_from_config"]:
                mat_changed.append(name)
            if key in rec.get("track_plaintext_keys", []):
                track_copy.append(name)
        by_key[key] = {
            "key": key,
            "profiles_present": sorted(present),
            "profiles_plaintext_suspect": sorted(plaintext),
            "profiles_reference_only": sorted(n for n in reference if n not in plaintext),
            "profiles_reference": sorted(reference),
            "profiles_path_ref": sorted(pathref),
            "profiles_suspicious_key_name": sorted(suspicious),
            "profiles_materialized": sorted(materialized),
            "profiles_materialized_unchanged": sorted(mat_unchanged),
            "profiles_materialized_changed": sorted(mat_changed),
            "profiles_track_holds_plaintext_copy": sorted(track_copy),
        }

    # ---- 按键路径聚合 ----
    by_path = {}
    for name, rec in per_profile.items():
        for f in rec["findings"]:
            k = (f["normalized"], f["classification"], f["reason"])
            slot = by_path.setdefault(k, {
                "normalized_path": f["normalized"],
                "key": f["key"],
                "classification": f["classification"],
                "reason": f["reason"],
                "profiles": [],
                "profiles_materialized": [],
            })
            slot["profiles"].append(name)
            if f["in_dash_inherited"]:
                slot["profiles_materialized"].append(name)
    path_rows = []
    for slot in by_path.values():
        slot["profiles"] = sorted(set(slot["profiles"]))
        slot["profiles_materialized"] = sorted(set(slot["profiles_materialized"]))
        slot["profile_count"] = len(slot["profiles"])
        slot["materialized_count"] = len(slot["profiles_materialized"])
        path_rows.append(slot)
    order = {CLS_PLAINTEXT: 0, CLS_REFERENCE: 1, CLS_PATH: 2, CLS_BENIGN: 3, CLS_EMPTY: 4}
    path_rows.sort(key=lambda r: (order.get(r["classification"], 9),
                                  -r["profile_count"], r["normalized_path"]))

    result = {
        "schema": "dashboard.inheritable_secret_audit.v1",
        "hermes_home": str(hermes_home),
        "inheritable_keys": list(INHERITABLE_KEYS),
        "inheritable_key_count": len(INHERITABLE_KEYS),
        "profiles_scanned": [n for n, _ in profiles],
        "profile_count": len(profiles),
        "yaml_parser": sorted(parsers_used) or ["none"],
        "files_read": files_read,
        "parse_problems": parse_problems,
        "by_key": [by_key[k] for k in INHERITABLE_KEYS],
        "by_path": path_rows,
        "per_profile": [
            {
                "profile": r["profile"],
                "config_yaml_present": r["config_yaml_present"],
                "config_yaml_is_symlink": r["config_yaml_is_symlink"],
                "track_present": r["track_present"],
                "keys_present_count": len(r["keys_present"]),
                "keys_present": r["keys_present"],
                "keys_in_track": r["keys_in_track"],
                "keys_track_matches_config": r["keys_track_matches_config"],
                "keys_track_differs_from_config": r["keys_track_differs_from_config"],
                "track_plaintext_keys": r["track_plaintext_keys"],
                "plaintext_finding_count": sum(
                    1 for f in r["findings"] if f["classification"] == CLS_PLAINTEXT),
                "reference_finding_count": sum(
                    1 for f in r["findings"] if f["classification"] == CLS_REFERENCE),
                "parser": r["parser"],
            }
            for r in (per_profile[n] for n, _ in profiles)
        ],
    }
    return result, build_guard_set(guard_raw, exclusion_terms)


def build_guard_set(guard_raw, exclusion_terms):
    """筛出「一旦出现在输出里就说明真的泄漏了」的值 -> {值: 分类}。

    纳入：`plaintext_suspect` 的整个值；`reference` 去掉 `${...}` 等占位符后的残余。
    排除：benign / path_ref / empty（含纯数字）、短于 MIN_GUARD_LEN 的值，
          以及等于任一 profile 名或键路径分段的值 —— 后者本来就会合法出现在报告里。
    """
    guard = {}
    for value, cls in guard_raw:
        if cls == CLS_PLAINTEXT:
            candidate = value.strip()
        elif cls == CLS_REFERENCE:
            candidate = reference_residue(value)
        else:
            continue
        if len(candidate) < MIN_GUARD_LEN:
            continue
        if candidate in exclusion_terms:
            continue
        guard[candidate] = cls
    return guard


# =========================================================================== #
# 5. 渲染
# =========================================================================== #

def _names(lst, limit=12):
    if not lst:
        return "—"
    if len(lst) <= limit:
        return ", ".join("`%s`" % n for n in lst)
    return ", ".join("`%s`" % n for n in lst[:limit]) + " …(+%d)" % (len(lst) - limit)


def render_markdown(res) -> str:
    L = []
    a = L.append
    a("# 继承键敏感值审计（R-06 / 基线 §5.5 / §19 Q-17）")
    a("")
    a("| 项 | 值 |")
    a("|---|---|")
    a("| HERMES_HOME | `%s` |" % res["hermes_home"])
    a("| 扫描 profile 数 | %d |" % res["profile_count"])
    a("| 继承键数 | %d |" % res["inheritable_key_count"])
    a("| YAML 解析器 | %s |" % ", ".join(res["yaml_parser"]))
    a("| 读取的文件数 | %d（只有 config.yaml 与 %s） |" % (len(res["files_read"]), INHERIT_TRACK))
    a("| 解析问题 | %d |" % len(res["parse_problems"]))
    a("")
    a("> **本报告只含键路径与计数，不含任何配置值。** 脚本在输出前做过"
      "「被判定为敏感的值（明文嫌疑值 + 引用值去掉 `${...}` 后的残余）不得出现在输出里」"
      "的自检；良性枚举与名字（如 `default`、`medium`）不纳入该检查，"
      "因为它们本来就会作为 profile 名与键路径分段合法出现。")
    a("> 读取范围严格限定为每个 profile 的 `config.yaml` 与 `%s`；"
      "`.env` / `credentials/` / `auth*` 从未被打开。" % INHERIT_TRACK)
    a("")

    # --- 1. 总览 ---
    a("## 1. 16 个继承键总览")
    a("")
    a("| # | 继承键 | 出现的 profile 数 | 含疑似明文凭据 | 含 `${ENV}` 引用 | 含可疑子键名 | 由仪表盘物化 | 物化台账里也有明文副本 |")
    a("|---:|---|---:|---:|---:|---:|---:|---:|")
    for i, row in enumerate(res["by_key"], 1):
        a("| %d | `%s` | %d | %d | %d | %d | %d | %d |" % (
            i, row["key"],
            len(row["profiles_present"]),
            len(row["profiles_plaintext_suspect"]),
            len(row["profiles_reference"]),
            len(row["profiles_suspicious_key_name"]),
            len(row["profiles_materialized"]),
            len(row["profiles_track_holds_plaintext_copy"]),
        ))
    a("")
    a("列义：")
    a("")
    a("- **含疑似明文凭据** = 该键子树里至少有一个叶子值命中凭据前缀 / 可疑子键名 + 非引用值 / 高熵长串。")
    a("- **含 `${ENV}` 引用** = 至少有一个叶子值是环境变量或 secret ref（**引用而非明文**）。")
    a("- **由仪表盘物化** = 该键出现在该 profile 的 `%s` 里，即 `_materialize_config` 沿组织树写进去的。" % INHERIT_TRACK)
    a("- **物化台账里也有明文副本** = `%s` 这个 JSON 自己也存了该键的疑似明文值"
      "（凭据的**第二份**落盘拷贝）。" % INHERIT_TRACK)
    a("")

    # --- 2. 明文嫌疑明细 ---
    plaintext_rows = [r for r in res["by_path"] if r["classification"] == CLS_PLAINTEXT]
    a("## 2. 疑似明文凭据的键路径（%d 条）" % len(plaintext_rows))
    a("")
    if not plaintext_rows:
        a("_没有命中。_")
    else:
        a("| 键路径 | 命中原因 | profile 数 | 其中由仪表盘物化 | profile |")
        a("|---|---|---:|---:|---|")
        for r in plaintext_rows:
            a("| `%s` | %s | %d | %d | %s |" % (
                r["normalized_path"], r["reason"], r["profile_count"],
                r["materialized_count"], _names(r["profiles"])))
    a("")

    # --- 3. 引用（非明文） ---
    ref_rows = [r for r in res["by_path"] if r["classification"] == CLS_REFERENCE]
    a("## 3. 引用而非明文的键路径（%d 条）" % len(ref_rows))
    a("")
    if not ref_rows:
        a("_没有命中。_")
    else:
        a("| 键路径 | 形式 | profile 数 | 其中由仪表盘物化 | profile |")
        a("|---|---|---:|---:|---|")
        for r in ref_rows:
            a("| `%s` | %s | %d | %d | %s |" % (
                r["normalized_path"], r["reason"], r["profile_count"],
                r["materialized_count"], _names(r["profiles"])))
    a("")

    other_rows = [r for r in res["by_path"]
                  if r["classification"] in (CLS_PATH, CLS_BENIGN, CLS_EMPTY)]
    if other_rows:
        a("## 4. 名字可疑但值形状不像凭据的键路径（%d 条，供复核）" % len(other_rows))
        a("")
        a("| 键路径 | 判定 | 原因 | profile 数 |")
        a("|---|---|---|---:|")
        for r in other_rows:
            a("| `%s` | %s | %s | %d |" % (
                r["normalized_path"], r["classification"], r["reason"], r["profile_count"]))
        a("")

    # --- 5. 物化台账 ---
    a("## 5. 物化台账（`%s`）" % INHERIT_TRACK)
    a("")
    a("| profile | config.yaml | 台账存在 | 台账追踪键数 | 仍纯继承 | 已被本地改写 | 台账含明文键 | 解析器 |")
    a("|---|---|---|---:|---:|---:|---:|---|")
    for p in res["per_profile"]:
        cfg = "symlink" if p["config_yaml_is_symlink"] else ("有" if p["config_yaml_present"] else "无")
        a("| `%s` | %s | %s | %d | %d | %d | %d | %s |" % (
            p["profile"], cfg, "有" if p["track_present"] else "无",
            len(p["keys_in_track"]), len(p["keys_track_matches_config"]),
            len(p["keys_track_differs_from_config"]), len(p["track_plaintext_keys"]),
            p["parser"] or "—"))
    a("")
    a("> `config.yaml` 列是 `symlink` 的 profile 是**分身（twin）**：它与根 profile 共享同一个"
      "文件（`server.py:_materialize_config` 对它直接 return，不物化）。它命中的凭据与根是"
      "**同一份**，不要重复计数。")
    a(">")
    a("> 「仍纯继承 / 已被本地改写」按 `config.yaml` 子树与台账子树的**拍平值比对**得出，"
      "与 `server.py:_own_config` 的 `track[k] == v` 判据同义但不是同一段代码；"
      "内置 YAML 解析器下类型可能被统一成文本，个别条目可能出现假的「已改写」。"
      "有 PyYAML 时（`/opt/homebrew/bin/python3.11`）此列最准。")
    a("")

    # --- 6. 解析问题 ---
    if res["parse_problems"]:
        a("## 6. 解析问题（这些 profile 的结论不完整）")
        a("")
        a("| profile | 文件 | 问题 |")
        a("|---|---|---|")
        for p in res["parse_problems"]:
            a("| `%s` | `%s` | %s |" % (p["profile"], p["file"], p["problem"]))
        a("")

    # --- 7. 结论 ---
    a("## 7. 结论（回答 §19 Q-17）")
    a("")
    keys_with_plain = [r for r in res["by_key"] if r["profiles_plaintext_suspect"]]
    keys_with_ref = [r for r in res["by_key"]
                     if r["profiles_reference"] and not r["profiles_plaintext_suspect"]]
    keys_materialized_plain = [r for r in keys_with_plain
                               if set(r["profiles_plaintext_suspect"]) & set(r["profiles_materialized"])]
    if keys_with_plain:
        a("- **携带疑似明文凭据的继承键（%d 个）**：%s" % (
            len(keys_with_plain), ", ".join("`%s`" % r["key"] for r in keys_with_plain)))
        for r in keys_with_plain:
            a("  - `%s`：%d/%d 个 profile 命中；其中 %d 个是仪表盘物化写入的。" % (
                r["key"], len(r["profiles_plaintext_suspect"]), len(r["profiles_present"]),
                len(set(r["profiles_plaintext_suspect"]) & set(r["profiles_materialized"]))))
    else:
        a("- **没有继承键命中疑似明文凭据。**")
    if keys_with_ref:
        a("- **只携带引用（`${ENV}` / secret ref）的继承键（%d 个）**：%s" % (
            len(keys_with_ref), ", ".join("`%s`" % r["key"] for r in keys_with_ref)))
    if keys_materialized_plain:
        a("- ⚠️ **明文凭据已经被 `_materialize_config` 沿组织树复制到子 profile**，"
          "涉及键：%s。这正是基线 §5.5 判定「现状违反 §5.4 目标纪律」的实证，"
          "Phase 5 的 Secret Ref 收敛必须覆盖这些键。" %
          ", ".join("`%s`" % r["key"] for r in keys_materialized_plain))
    track_plain = [r for r in res["by_key"] if r["profiles_track_holds_plaintext_copy"]]
    if track_plain:
        a("- ⚠️ `%s` 里另存了 %s 的疑似明文值 —— 同一份凭据在磁盘上**至少两份**"
          "（config.yaml 一份、台账一份），迁移与清理时两处都要处理。" %
          (INHERIT_TRACK, ", ".join("`%s`" % r["key"] for r in track_plain)))
    a("")
    a("_口径说明：本脚本做的是**形状**判定，不做在线校验。"
      "「疑似明文凭据」= 值的形状像凭据，不等于它一定有效；"
      "反过来，自定义格式的凭据也可能被漏判。人工复核以键路径为准。_")
    return "\n".join(L) + "\n"


# =========================================================================== #
# 6. 泄漏自检
# =========================================================================== #

class ValueLeak(RuntimeError):
    pass


def assert_no_values_leaked(output: str, guard, min_len=MIN_GUARD_LEN):
    """输出里绝不能出现**敏感**值。

    `guard` 由 `build_guard_set()` 产出：{值: 分类}，只含 `plaintext_suspect` 的值
    与 `reference` 的占位符残余，且已滤掉短值与「等于 profile 名 / 键路径分段」的值。
    也接受一个纯字符串集合（分类记为 unknown），方便单测。

    良性值**不纳入**：`default` / `medium` / `deepseek` 这类枚举与名字同时会合法地
    出现在报告里（profile 名、键路径分段、章节文字），拿它们当泄漏证据是误报。

    报错信息只带分类与长度，**绝不带值本身**。
    """
    items = guard.items() if isinstance(guard, dict) else ((v, "unknown") for v in guard)
    for value, cls in items:
        if not isinstance(value, str):
            continue
        s = value.strip()
        if len(s) < min_len:
            continue
        if s.lower() in ("true", "false", "null", "none", "{}", "[]"):
            continue
        if s in output:
            raise ValueLeak(
                "输出中出现了被判定为敏感的值（分类 %s，长度 %d），已中止；"
                "值本身不打印。请检查 render_markdown/JSON 是否误把值写进了报告。"
                % (cls, len(s)))


# =========================================================================== #
# 7. fixture / self-test
# =========================================================================== #

FIXTURE_SECRETS = {
    "root_anthropic": "sk-ant-api03-ZmFrZUZBS0VmYWtlRkFLRTEyMzQ1Njc4OTBhYmNkZWY",
    "root_openai": "sk-proj-QWERTYuiop1234567890ZXCVBNMasdfgh",
    "alpha_github": "ghp_abcdefGHIJKL0123456789mnopQRSTuvwx",
    "beta_random": "Zm9vYmFyLXNlY3JldC1LRVktOTk5OTk5OTk5OQ",
    "gamma_aws": "AKIAIOSFODNN7EXAMPLE",
}


def build_fixture(root: Path):
    """造一个隔离的假 HERMES_HOME，覆盖：明文 / 引用 / 路径 / 无凭据 / 台账 / 坏 YAML。"""
    root = Path(root)
    (root / "profiles").mkdir(parents=True, exist_ok=True)

    # default（根）—— 明文 provider key + 正常配置
    (root / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    api_key: %s\n"
        "    base_url: https://api.anthropic.com\n"
        "  openai:\n"
        "    api_key: %s\n"
        "credential_pool_strategies:\n"
        "  rotation: round_robin\n"
        "toolsets:\n"
        "- read\n"
        "- write\n"
        "agent:\n"
        "  reasoning_effort: high\n"
        # 良性枚举/名字：它们同时是 profile 名、键路径分段、报告里的普通词，
        # 泄漏守卫绝不能把它们当成"值泄漏"（回归：ValueLeak 长度 7 的误报）。
        "  profile_name: default\n"
        "  provider_alias: deepseek\n"
        "  effort_label: medium\n"
        "compression:\n"
        "  enabled: true\n"
        "model: claude-opus-4-20250514\n"
        % (FIXTURE_SECRETS["root_anthropic"], FIXTURE_SECRETS["root_openai"]),
        encoding="utf-8")
    # 这些文件必须永远不被脚本打开
    (root / ".env").write_text("OPENAI_API_KEY=sk-should-never-be-read\n", encoding="utf-8")
    (root / "credentials").mkdir(exist_ok=True)
    (root / "credentials" / "oauth.json").write_text("{}\n", encoding="utf-8")
    (root / "auth.json").write_text("{}\n", encoding="utf-8")

    # alpha —— 被物化过：config 与台账都有 providers 的明文；mcp_servers 里是明文 token
    alpha = root / "profiles" / "alpha"
    alpha.mkdir(parents=True, exist_ok=True)
    (alpha / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    api_key: %s\n"
        "    base_url: https://api.anthropic.com\n"
        "mcp_servers:\n"
        "  github:\n"
        "    command: npx\n"
        "    env:\n"
        "      GITHUB_TOKEN: %s\n"
        "toolsets:\n"
        "- read\n"
        "agent:\n"
        "  reasoning_effort: medium\n"
        % (FIXTURE_SECRETS["root_anthropic"], FIXTURE_SECRETS["alpha_github"]),
        encoding="utf-8")
    (alpha / INHERIT_TRACK).write_text(json.dumps({
        "providers": {
            "anthropic": {"api_key": FIXTURE_SECRETS["root_anthropic"],
                          "base_url": "https://api.anthropic.com"}},
        "toolsets": ["read"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # beta —— 只有引用，没有明文；台账里的 toolsets 已被本地改写
    beta = root / "profiles" / "beta"
    beta.mkdir(parents=True, exist_ok=True)
    (beta / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    api_key: ${ANTHROPIC_API_KEY}\n"
        "mcp_servers:\n"
        "  local:\n"
        "    command: /usr/local/bin/mcp-local\n"
        "    env:\n"
        "      TOKEN_FILE: ~/.config/mcp/token.txt\n"
        "toolsets:\n"
        "- read\n"
        "- write\n"
        "curator:\n"
        "  enabled: false\n",
        encoding="utf-8")
    (beta / INHERIT_TRACK).write_text(json.dumps({
        "toolsets": ["read"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # gamma —— 高熵串 + AWS key，且没有台账
    gamma = root / "profiles" / "gamma"
    gamma.mkdir(parents=True, exist_ok=True)
    (gamma / "config.yaml").write_text(
        "auxiliary:\n"
        "  s3:\n"
        "    access_key_id: %s\n"
        "    region: us-east-1\n"
        "hooks:\n"
        "  pre_tool:\n"
        "  - name: audit\n"
        "    command: /usr/bin/true\n"
        "delegation:\n"
        "  reasoning_effort: low\n"
        "image_gen:\n"
        "  provider_secret: %s\n"
        % (FIXTURE_SECRETS["gamma_aws"], FIXTURE_SECRETS["beta_random"]),
        encoding="utf-8")

    # delta —— 坏 YAML，必须被报成解析问题而不是"没有凭据"
    delta = root / "profiles" / "delta"
    delta.mkdir(parents=True, exist_ok=True)
    (delta / "config.yaml").write_text(
        "providers:\n"
        "\t- broken tab indent\n"
        "  : also broken\n",
        encoding="utf-8")

    # epsilon —— 完全没有继承键
    eps = root / "profiles" / "epsilon"
    eps.mkdir(parents=True, exist_ok=True)
    (eps / "config.yaml").write_text("model: gpt-5\n", encoding="utf-8")
    return root


def self_test() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = build_fixture(Path(td) / "hermes")
        res, guard = audit(root)
        md = render_markdown(res)
        js = json.dumps(res, ensure_ascii=False, indent=2)
        checks = []

        def check(name, cond):
            checks.append((name, bool(cond)))

        check("扫描到 6 个 profile", res["profile_count"] == 6)
        check("16 个继承键", res["inheritable_key_count"] == 16)
        by_key = {r["key"]: r for r in res["by_key"]}
        check("providers 出现在 default/alpha/beta",
              {"default", "alpha", "beta"} <= set(by_key["providers"]["profiles_present"]))
        check("epsilon 没有任何继承键",
              all("epsilon" not in r["profiles_present"] for r in res["by_key"]))
        check("providers 有明文命中", len(by_key["providers"]["profiles_plaintext_suspect"]) >= 2)
        check("providers 的 beta 只算引用",
              "beta" in by_key["providers"]["profiles_reference"]
              and "beta" not in by_key["providers"]["profiles_plaintext_suspect"])
        check("mcp_servers 有明文命中（alpha）",
              "alpha" in by_key["mcp_servers"]["profiles_plaintext_suspect"])
        check("auxiliary 有明文命中（gamma）",
              "gamma" in by_key["auxiliary"]["profiles_plaintext_suspect"])
        check("providers 被物化到 alpha",
              by_key["providers"]["profiles_materialized"] == ["alpha"])
        check("台账里也存了 providers 的明文",
              by_key["providers"]["profiles_track_holds_plaintext_copy"] == ["alpha"])
        check("toolsets 在 beta 被判为本地改写",
              "beta" in by_key["toolsets"]["profiles_materialized_changed"])
        check("坏 YAML 被报成解析问题",
              any(p["profile"] == "delta" for p in res["parse_problems"]))
        check("只读了 config.yaml / %s" % INHERIT_TRACK,
              all(Path(f).name in ("config.yaml", INHERIT_TRACK) for f in res["files_read"]))
        leaked = [s for s in FIXTURE_SECRETS.values() if s in md or s in js]
        check("markdown/JSON 里没有任何凭据值", not leaked)
        check("守卫集合只含敏感值，不含 default/medium/deepseek 这类良性名字",
              not ({"default", "medium", "deepseek", "high"} & set(guard)))
        check("守卫集合覆盖了 fixture 里的每一个凭据",
              set(FIXTURE_SECRETS.values()) <= set(guard))
        try:
            assert_no_values_leaked(md + js, guard)
            check("泄漏自检通过（良性枚举不触发误报）", True)
        except ValueLeak:
            check("泄漏自检通过（良性枚举不触发误报）", False)

        ok = all(c for _, c in checks)
        for name, c in checks:
            sys.stderr.write("  %s %s\n" % ("PASS" if c else "FAIL", name))
        sys.stderr.write("self-test: %s（%d/%d）\n"
                         % ("OK" if ok else "FAILED",
                            sum(1 for _, c in checks if c), len(checks)))
        return 0 if ok else 1


# =========================================================================== #
# 8. main
# =========================================================================== #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="继承键敏感值审计（R-06）——只输出键路径与计数，绝不输出值。")
    ap.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME"),
                    help="Hermes home（默认 $HERMES_HOME，再默认 ~/.hermes）")
    ap.add_argument("--profile", action="append", default=None,
                    help="只审计这些 profile（可重复）")
    ap.add_argument("--json", action="store_true",
                    help="输出 JSON 到 stdout（代替 markdown）")
    ap.add_argument("--json-out", metavar="PATH", default=None,
                    help="额外把 JSON 写到文件（markdown 仍然进 stdout）")
    ap.add_argument("--md-out", metavar="PATH", default=None,
                    help="额外把 markdown 写到文件")
    ap.add_argument("--parser", choices=("auto", "builtin"), default="auto",
                    help="YAML 解析器：auto=有 PyYAML 就用（默认），builtin=强制用内置最小解析器")
    ap.add_argument("--self-test", action="store_true",
                    help="用内置临时 fixture 自检（不碰真实 HERMES_HOME）")
    ap.add_argument("--fail-on-plaintext", action="store_true",
                    help="发现疑似明文凭据时以退出码 4 结束（给 CI 用）")
    args = ap.parse_args(argv)

    global FORCE_PARSER
    FORCE_PARSER = args.parser

    if args.self_test:
        return self_test()

    home = Path(args.hermes_home).expanduser() if args.hermes_home else Path.home() / ".hermes"
    if not home.is_dir():
        sys.stderr.write("HERMES_HOME 不存在: %s\n" % home)
        return 1

    res, guard = audit(home, only_profiles=args.profile)
    md = render_markdown(res)
    js = json.dumps(res, ensure_ascii=False, indent=2)

    # 输出前的硬性纪律检查：被判定为敏感的值（明文嫌疑 + 引用里的实体残余）
    # 一个都不许出现在输出里。良性枚举/名字不纳入 —— 它们本来就会合法出现。
    assert_no_values_leaked(md + js, guard)

    if args.json_out:
        Path(args.json_out).write_text(js + "\n", encoding="utf-8")
    if args.md_out:
        Path(args.md_out).write_text(md, encoding="utf-8")

    sys.stdout.write(js + "\n" if args.json else md)

    if args.fail_on_plaintext and any(r["profiles_plaintext_suspect"] for r in res["by_key"]):
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
