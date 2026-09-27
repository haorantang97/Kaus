#!/usr/bin/env python3
"""
Hermes Delegate MCP —— 让一个 agent 把另一个 agent「当工具直接调用」（横向协作 / expose_as_tool）。

和「借技能」的区别：
  借技能 = 把对方的 skill symlink 过来，自己用自己的脑子做（一个上下文）。
  直调   = 以对方的身份（它的人格 / 记忆 / 技能 / MCP）真实跑一次，拿它本人的判断（两个上下文）。

工具：
  call_agent(target, prompt)  以 target profile 跑一次 hermes oneshot，返回它的回答。

触发方式（重要）：
  对话窗里自然语言触发 —— 调用方 agent 的 LLM 看到这个工具，会在"需要某个 agent 的专业判断"时
  自主决定调用它（和调用任何 MCP 工具 / 触发技能一样），用户不用手动接线。
  例：你对活动策划说"这场活动财务上扛得住吗，问下财务"，它就会调 call_agent(target=trading, ...)。

协议：JSON-RPC 2.0 over stdio，MCP 2024-11-05。仅标准库。
环境变量：HERMES_PROFILE（可选，自身名，用于防自调）/ HERMES_HOME（推断自身名）/ HERMES_BIN
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


HERMES_BIN = os.environ.get("HERMES_BIN") or os.path.expanduser("~/.local/bin/hermes")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_CALL_TIMEOUT = 180  # fallback when config is unavailable


def _root_hermes_home() -> Path:
    """Return the root Hermes home even when this MCP runs under a profile."""
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        try:
            p = Path(home).expanduser().resolve()
            if p.parent.name == "profiles":
                return p.parent.parent
            return p
        except (OSError, RuntimeError):
            pass
    return Path.home() / ".hermes"


def _load_root_env() -> dict[str, str]:
    """Load root ~/.hermes/.env names into delegated hermes subprocesses.

    `hermes -p <target>` switches HERMES_HOME to the target profile, whose
    directory usually has no .env. Without this, profile-to-profile delegation
    can fail with "Provider ... no API key" even though the key exists in the
    root Hermes .env and dashboard PTY launches work.
    """
    env_path = _root_hermes_home() / ".env"
    try:
        text = env_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    result: dict[str, str] = {}
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


def _read_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _profile_config_path(profile: str) -> Path:
    home = _root_hermes_home()
    if profile == "default":
        return home / "config.yaml"
    return home / "profiles" / profile / "config.yaml"


def _model_entry(value: Any) -> dict[str, str]:
    """Normalize the model shape accepted by Hermes config."""
    if isinstance(value, str) and value.strip():
        return {"default": value.strip()}
    if not isinstance(value, dict):
        return {}
    model = str(value.get("default") or "").strip()
    if not model:
        return {}
    out = {"default": model}
    provider = str(value.get("provider") or "").strip()
    if provider:
        out["provider"] = provider
    return out


def _effective_model(target: str) -> dict[str, str]:
    """Resolve Dashboard soft model inheritance for a delegated profile.

    Dashboard-launched terminals inject the effective inherited model, while a
    raw ``hermes -p <target> -z`` process only sees the target's physical
    config.  Prefer the live Dashboard's canonical resolver, then fall back to
    walking hierarchy.json so delegation also works when the UI is closed.
    """
    try:
        url = f"http://127.0.0.1:8877/api/agent/{target}/levers"
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        resolved = _model_entry(payload.get("model"))
        if resolved:
            return resolved
    except (OSError, ValueError, urllib.error.URLError):
        pass

    root = _root_hermes_home()
    try:
        hierarchy = json.loads((root / "dashboard" / "hierarchy.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hierarchy = {}

    current = target
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        resolved = _model_entry(_read_yaml(_profile_config_path(current)).get("model"))
        if resolved:
            return resolved
        if current == "default":
            break
        parent = hierarchy.get(current) if isinstance(hierarchy, dict) else None
        current = str(parent).strip() if parent else "default"
    return {}


def _configured_call_timeout() -> int:
    """Resolve delegate timeout from config, matching Hermes MCP timeout semantics.

    The outer Hermes MCP client reads `mcp_servers.delegate.timeout`; this MCP
    server also reads the same value so sync `call_agent` and the MCP wrapper
    share one timeout budget. If unavailable, fall back to
    `delegation.child_timeout_seconds`, then DEFAULT_CALL_TIMEOUT.
    """
    env_timeout = os.environ.get("HERMES_DELEGATE_TIMEOUT", "").strip()
    if env_timeout.isdigit():
        return max(1, int(env_timeout))

    candidates = []
    if SELF:
        candidates.append(_profile_config_path(SELF))
    candidates.append(_root_hermes_home() / "config.yaml")
    for path in candidates:
        cfg = _read_yaml(path)
        if not isinstance(cfg, dict):
            continue
        delegate_cfg = ((cfg.get("mcp_servers") or {}).get("delegate") or {})
        if isinstance(delegate_cfg, dict):
            timeout = delegate_cfg.get("timeout")
            if isinstance(timeout, (int, float)) and timeout > 0:
                return int(timeout)
        delegation_cfg = cfg.get("delegation") or {}
        if isinstance(delegation_cfg, dict):
            timeout = delegation_cfg.get("child_timeout_seconds")
            if isinstance(timeout, (int, float)) and timeout > 0:
                return int(timeout)
    return DEFAULT_CALL_TIMEOUT


def _detect_profile() -> str:
    explicit = os.environ.get("HERMES_PROFILE", "").strip()
    if explicit:
        return explicit
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        return ""
    try:
        p = Path(home).resolve()
    except (OSError, RuntimeError):
        return ""
    if p.parent.name == "profiles":
        return p.name
    if p.name in (".hermes", "hermes"):
        return "default"
    return p.name


SELF = _detect_profile()
CALL_TIMEOUT = _configured_call_timeout()

TOOLS = [
    {
        "name": "call_agent",
        "description": "把另一个 agent 当工具直接调用：以它的身份（人格/记忆/技能/MCP）真实跑一次，返回它本人的回答。"
                       "用在需要某个其他 agent 的专业判断时（不是借它技能自己做，而是让它本人来答）。"
                       "注意：会真实运行那个 agent（消耗模型调用、可能触发它自己的工具，可能较慢）。"
                       "调用前建议用 dashboard 工具的 org_tree / agent_detail 确认目标存在且未停用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标 agent 的 ID（小写，如 trading / law）"},
                "prompt": {"type": "string", "description": "要问它的问题 / 交给它的任务（自然语言）"},
            },
            "required": ["target", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delegate_task",
        "description": "把长任务提交到 Hermes Kanban，而不是在 MCP 调用里同步等待。"
                       "适合跨 agent 长流程、需要后台执行/人工审核/可追踪进度的任务。"
                       "返回 task id 后，调用方应通过 kanban/任务板跟踪，而不是反复同步 call_agent。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "目标 assignee agent 的 ID（如 web-auto-screening）"},
                "title": {"type": "string", "description": "任务标题；不传则从 prompt 第一行生成"},
                "prompt": {"type": "string", "description": "任务正文 / 要交给目标 agent 的要求"},
                "priority": {"type": "integer", "description": "可选优先级"},
                "triage": {"type": "boolean", "description": "true=先进 triage 等人工/规格确认；false=进入可调度队列，默认 false"},
                "goal": {"type": "boolean", "description": "true=使用 goal loop 让 worker 多轮推进，适合开放式长任务"},
                "goal_max_turns": {"type": "integer", "description": "goal loop 最大轮数，可选"},
                "max_runtime": {"type": "string", "description": "可选运行上限，如 30m / 2h"},
                "idempotency_key": {"type": "string", "description": "可选去重键，避免重复建卡"},
            },
            "required": ["target", "prompt"],
            "additionalProperties": False,
        },
    },
]


def _text(s):
    return {"content": [{"type": "text", "text": s}]}


def _err(s):
    return {"content": [{"type": "text", "text": s}], "isError": True}


def _validate_target(target: str) -> str | None:
    if not target or (target != "default" and not PROFILE_RE.match(target)):
        return f"非法 target: {target!r}"
    if SELF and target == SELF:
        return "不能调用自己（防止自调死循环）"
    profile_dir = _root_hermes_home() if target == "default" else _root_hermes_home() / "profiles" / target
    if not profile_dir.is_dir():
        return f"目标 agent 不存在: {target}"
    return None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for _k, _v in _ROOT_ENV.items():
        env.setdefault(_k, _v)
    return env


def _short_title(prompt: str) -> str:
    first = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
    if not first:
        return "Delegated task"
    return first[:72]


def _call_agent(args):
    target = (args.get("target") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    problem = _validate_target(target)
    if problem:
        return _err(problem)
    if not prompt:
        return _err("prompt 不能为空")
    argv = [HERMES_BIN] + (["-p", target] if target != "default" else [])
    model = _effective_model(target)
    if model.get("provider"):
        argv += ["--provider", model["provider"]]
    if model.get("default"):
        argv += ["-m", model["default"]]
    argv += ["-z", prompt]
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=CALL_TIMEOUT, env=_env())
    except FileNotFoundError:
        return _err(f"找不到 hermes: {HERMES_BIN}")
    except subprocess.TimeoutExpired:
        return _err(f"调用 {target} 超时（>{CALL_TIMEOUT}s）")
    if p.returncode != 0:
        return _err(f"调用 {target} 失败: {(p.stderr or p.stdout or '未知错误').strip()[:400]}")
    out = (p.stdout or "").strip()
    return _text(f"【{target} 的回答】\n{out or '(空响应)'}")


def _delegate_task(args):
    target = (args.get("target") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    problem = _validate_target(target)
    if problem:
        return _err(problem)
    if not prompt:
        return _err("prompt 不能为空")

    title = (args.get("title") or "").strip() or _short_title(prompt)
    argv = [
        HERMES_BIN,
        "kanban",
        "create",
        title,
        "--assignee",
        target,
        "--body",
        prompt,
        "--created-by",
        SELF or "hermes-delegate",
        "--json",
    ]
    priority = args.get("priority")
    if isinstance(priority, int):
        argv += ["--priority", str(priority)]
    if args.get("triage") is True:
        argv.append("--triage")
    if args.get("goal") is True:
        argv.append("--goal")
    goal_max_turns = args.get("goal_max_turns")
    if isinstance(goal_max_turns, int) and goal_max_turns > 0:
        argv += ["--goal-max-turns", str(goal_max_turns)]
    max_runtime = (args.get("max_runtime") or "").strip()
    if max_runtime:
        argv += ["--max-runtime", max_runtime]
    idempotency_key = (args.get("idempotency_key") or "").strip()
    if idempotency_key:
        argv += ["--idempotency-key", idempotency_key]

    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=_env())
    except FileNotFoundError:
        return _err(f"找不到 hermes: {HERMES_BIN}")
    except subprocess.TimeoutExpired:
        return _err("创建 Kanban 任务超时（>30s），未确认任务是否创建成功；请查 kanban list")
    if p.returncode != 0:
        return _err(f"创建 Kanban 任务失败: {(p.stderr or p.stdout or '未知错误').strip()[:500]}")
    try:
        task = json.loads((p.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        task = {"raw": (p.stdout or "").strip()[:500]}
    tid = task.get("id") or task.get("task_id") or task.get("task", {}).get("id") or "未知"
    status = task.get("status") or task.get("task", {}).get("status") or "已创建"
    return _text(
        f"已提交异步委派任务，不再阻塞等待子 agent 完整跑完。\n"
        f"target: {target}\n"
        f"task_id: {tid}\n"
        f"status: {status}\n"
        f"title: {title}\n\n"
        f"后续请在 Kanban/任务板跟踪该 task；需要立即派发时触发 kanban dispatch。"
    )


def call_tool(name, args):
    if name == "call_agent":
        return _call_agent(args)
    if name == "delegate_task":
        return _delegate_task(args)
    return _err(f"未知工具: {name}")


def handle_request(req):
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "hermes-delegate", "version": "0.1.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid,
                "result": call_tool(params.get("name", ""), params.get("arguments") or {})}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    print(f"hermes-delegate MCP 启动 · self={SELF} · hermes={HERMES_BIN} · timeout={CALL_TIMEOUT}s", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"解析失败: {e}\n")
            continue
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
