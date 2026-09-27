#!/usr/bin/env python3
"""
Kaus MCP server —— 把仪表盘的组织治理能力暴露给 agent。

为什么需要：组织树（谁是谁的 parent/child）是仪表盘维护的元数据，hermes 本身不感知。
没有它，agent 在对话里无法"沿组织树把任务派给我的下属"——因为它根本不知道谁是它的下属。
这个 MCP 把组织树 + 治理视图喂给 agent，补上任务路由的能力缺口。

工具（全部只读）：
  org_tree           整棵组织树（所有 agent 的 parent/children/role/killed）
  my_position        【任务路由核心】我是谁的子、谁是我的子/后代、我的祖先链、我是否被停用
  agent_detail(name) 某个 agent 的详情（parent/children/role/model/killed/技能数）——派活前先核查
  dashboard_summary  系统健康 / 待 Review / 最近变更 三卡聚合
  lint_check(name?)  规范体检（全网或单个 profile）
  agent_impact(name) 改某 agent 会波及哪些后代（config / external_dirs / vault / symlink）

协议：JSON-RPC 2.0 over stdio，MCP 2024-11-05。仅标准库。

环境变量：
  HERMES_PROFILE        可选，显式指定当前 agent；不填则从 HERMES_HOME 自动推断
  KAUS_DASHBOARD_URL    默认 http://127.0.0.1:8877
  HERMES_DASHBOARD_URL  旧名称，继续兼容

配置示例（已加进 default/config.yaml，分身 symlink + 子孙沿树继承，全员可用）：
  mcp_servers:
    dashboard:
      command: python3
      args: ["/path/to/Kaus/mcp_servers/hermes_dashboard_mcp.py"]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DASHBOARD = (
    os.environ.get("KAUS_DASHBOARD_URL")
    or os.environ.get("HERMES_DASHBOARD_URL")
    or "http://127.0.0.1:8877"
).rstrip("/")
PROTOCOL_VERSION = "2024-11-05"


def _detect_profile() -> str:
    """同 vault MCP：HERMES_PROFILE 显式 > 从 HERMES_HOME 路径推断 > 空。"""
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


PROFILE = _detect_profile()

TOOLS = [
    {
        "name": "org_tree",
        "description": "返回整棵 agent 组织树：每个 agent 的 parent / children / role / 是否停用。"
                       "用于了解全局组织结构。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "my_position",
        "description": "【任务路由核心】返回当前 agent 在组织树里的位置：我的 parent、我的直接 children、"
                       "我的全部后代、我的祖先链、我是否被停用。要把任务沿组织树派给下属时先调它拿到下属名单。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "agent_detail",
        "description": "返回某个 agent 的详情：parent / children / role / model / 是否停用 / 技能数。"
                       "把任务派给某 agent 前，先用它核查对方是否存在、是否被停用、有没有相关能力。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "目标 agent 名"}},
            "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "dashboard_summary",
        "description": "系统健康（agent 计数 / lint / MCP / 宪法）+ 待 Review（停用 / 硬违规 / 草稿）"
                       "+ 最近变更 三卡聚合。给管理型 agent（如 architect / steward）做全局巡检用。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "lint_check",
        "description": "规范体检：校验 skill frontmatter（name/description、连字符命名）和 mcp_servers 名。"
                       "不传 name = 全网汇总；传 name = 单个 profile。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "可选，单个 profile 名；不传则全网"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_impact",
        "description": "改某 agent 会波及哪些后代：config 继承 / external_dirs 技能继承 / vault / 横向 symlink。"
                       "改动一个上游 agent 前，先评估影响范围。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "目标 agent 名"}},
            "required": ["name"], "additionalProperties": False,
        },
    },
]


def _http_get(path: str) -> dict:
    url = f"{DASHBOARD}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:  # noqa: BLE001
            detail = str(e)
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 {DASHBOARD}: {e.reason}") from e


def _descendants(name: str, nodes: dict) -> list:
    """从 nodes 里收集 name 的全部后代（BFS）。"""
    out, stack = [], list(nodes.get(name, {}).get("children", []))
    seen = set()
    while stack:
        n = stack.pop(0)
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
        stack.extend(nodes.get(n, {}).get("children", []))
    return out


def _ancestors(name: str, nodes: dict) -> list:
    """从 nodes 里沿 parent 上溯收集祖先链（近→远）。"""
    out, cur, seen, guard = [], nodes.get(name, {}).get("parent"), set(), 0
    while cur and cur not in seen and guard < 30:
        seen.add(cur); guard += 1
        out.append(cur)
        cur = nodes.get(cur, {}).get("parent")
    return out


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _err(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}], "isError": True}


def call_tool(name: str, args: dict) -> dict:
    try:
        if name == "org_tree":
            d = _http_get("/api/network")
            nodes = d.get("nodes", {})
            lines = []
            def walk(n, depth):
                node = nodes.get(n, {})
                flags = []
                if node.get("main_twin"):
                    flags.append("分身")
                if node.get("killed") or node.get("effective_killed"):
                    flags.append("停用")
                tag = f" [{'/'.join(flags)}]" if flags else ""
                lines.append("  " * depth + f"- {n}{tag}")
                for c in sorted(node.get("children", [])):
                    walk(c, depth + 1)
            for root in d.get("roots", []):
                walk(root, 0)
            drafts = [x["name"] for x in d.get("drafts", [])]
            extra = f"\n\n草稿（未落位）：{', '.join(drafts)}" if drafts else ""
            return _text("组织树：\n" + "\n".join(lines) + extra)

        if name == "my_position":
            if not PROFILE:
                return _err("无法确定当前 agent（HERMES_PROFILE / HERMES_HOME 都没有）")
            d = _http_get("/api/network")
            nodes = d.get("nodes", {})
            if PROFILE not in nodes:
                drafts = [x["name"] for x in d.get("drafts", [])]
                if PROFILE in drafts:
                    return _text(f"{PROFILE} 当前是草稿（未落位到组织树），没有 parent / children。")
                return _err(f"{PROFILE} 不在组织树里")
            me = nodes[PROFILE]
            children = sorted(me.get("children", []))
            desc = sorted(_descendants(PROFILE, nodes))
            anc = _ancestors(PROFILE, nodes)
            killed = me.get("killed") or me.get("effective_killed")
            return _text(
                f"我是 {PROFILE}\n"
                f"parent: {me.get('parent') or '（无，我是树根）'}\n"
                f"直接下属（children）: {', '.join(children) if children else '（无）'}\n"
                f"全部后代: {', '.join(desc) if desc else '（无）'}\n"
                f"祖先链（近→远）: {', '.join(anc) if anc else '（无）'}\n"
                f"停用状态: {'已停用' if killed else '启用中'}\n\n"
                f"派活提示：要沿组织树把任务交给下属，从上面的 children / 后代里选；"
                f"派之前可用 agent_detail 核查对方是否停用、有无相关技能。"
            )

        if name == "agent_detail":
            target = (args.get("name") or "").strip()
            if not target:
                return _err("缺少 name 参数")
            d = _http_get(f"/api/profile/{urllib.parse.quote(target)}")
            net = _http_get("/api/network")
            node = net.get("nodes", {}).get(target, {})
            children = sorted(node.get("children", []))
            return _text(
                f"{target}\n"
                f"parent: {d.get('parent') or '（无）'}\n"
                f"children: {', '.join(children) if children else '（无）'}\n"
                f"role: {d.get('role')}\n"
                f"model: {d.get('model') or '—'}\n"
                f"停用: {'是' if d.get('effective_killed') else '否'}\n"
                f"技能数: {d.get('skill_count')}\n"
                f"分身: {'是' if d.get('main_twin') else '否'}"
            )

        if name == "dashboard_summary":
            d = _http_get("/api/dashboard/summary")
            h = d["health"]
            rv = d["review"]
            hard_profiles = ", ".join(f"{x['profile']}({x['count']})" for x in rv["hard_lint"]) or "无"
            return _text(
                f"系统健康：{h['agent_total']} agent（主 {h['main']} / 分身 {h['twin']} / 子 {h['sub']}）"
                f"· 停用 {h['killed']} · lint {h['lint_hard_total']} 硬 / {h['lint_soft_total']} 软"
                f"· MCP {h['default_mcp_count']} 个 / {h['agents_with_mcp']} agent 继承"
                f"· 宪法 {'未启用' if h['const_subscribed'] == 0 else str(h['const_subscribed']) + ' agent'}\n"
                f"待 Review：停用 {rv['killed'] or '无'} · 硬违规 {hard_profiles} · 草稿 {rv['drafts'] or '无'}\n"
                f"最近变更 {len(d['recent_changes'])} 条，最近："
                f"{d['recent_changes'][0]['ago'] + ' ' + d['recent_changes'][0]['target'] if d['recent_changes'] else '无'}"
            )

        if name == "lint_check":
            target = (args.get("name") or "").strip()
            if target:
                d = _http_get(f"/api/lint/{urllib.parse.quote(target)}")
                hard = d.get("hard", [])
                soft = d.get("soft", [])
                body = "\n".join(f"  [硬] {v['where']}: {v['msg']}" for v in hard[:10])
                body += "\n" + "\n".join(f"  [软] {v['where']}: {v['msg']}" for v in soft[:10])
                return _text(f"{target} 规范体检：{d.get('hard_count')} 硬 / {d.get('soft_count')} 软\n{body.strip() or '✓ 合规'}")
            d = _http_get("/api/lint")
            rows = sorted(d.get("profiles", []), key=lambda p: -p["hard_count"])
            lines = [f"  {p['name']}: {p['hard_count']} 硬 / {p['soft_count']} 软" for p in rows if p["hard_count"] or p["soft_count"]]
            return _text(f"全网规范体检：合计 {d['total_hard']} 硬 / {d['total_soft']} 软\n" + ("\n".join(lines) or "✓ 全员合规"))

        if name == "agent_impact":
            target = (args.get("name") or "").strip()
            if not target:
                return _err("缺少 name 参数")
            d = _http_get(f"/api/profile/{urllib.parse.quote(target)}/impact")
            s = d["summary"]
            sym = ", ".join(f"{c['name']}({len(c['skills'])})" for c in d["symlink_consumers"]) or "无"
            return _text(
                f"改 {target} 的影响范围：\n"
                f"沿树后代 {s['descendants_count']} 个：{', '.join(d['descendants']) or '无'}\n"
                f"  · config 继承受影响 {s['config_affected']}\n"
                f"  · external_dirs 技能继承 {s['external_dirs_count']}\n"
                f"  · vault 继承 {s['vault_descendants_count']}\n"
                f"横向 symlink 共享我技能的 {s['symlink_consumer_count']} 个：{sym}"
            )

        return _err(f"未知工具: {name}")
    except RuntimeError as e:
        return _err(f"调用失败: {e}")


def handle_request(req: dict):
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "kaus-dashboard", "version": "0.1.0"},
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
    print(f"kaus-dashboard MCP 启动 · profile={PROFILE} · dashboard={DASHBOARD}", file=sys.stderr)
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
