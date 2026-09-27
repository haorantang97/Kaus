#!/usr/bin/env python3.11
"""
真机验证脚本（交给 Code 模式 / 在 Mac 上跑）。

用法：
    cd ~/.hermes/dashboard
    /opt/homebrew/bin/python3.11 test_dashboard.py

它做三件事：
  1. 用从 hermes 源码提取的【精确格式】回放 `profile list` / `sessions list`，
     测试 server.py 里的解析器（格式契约测试，离线即可）。
  2. 对真实 ~/.hermes 跑 FastAPI 端到端（角色分类、详情、隔离、防注入）。
  3. 关键校准诊断：本机有 hermes 但数据源却回退成 filesystem，
     说明 parse_profile_list 没命中 v0.14.0 的真实输出 —— 需要按真实表头微调。
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 不强制 HERMES_HOME —— 让 server.py 默认 ~/.hermes（真机正解）
import server  # noqa: E402

fails = []
warns = []


def check(label, cond):
    print(("PASS" if cond else "FAIL"), "-", label)
    if not cond:
        fails.append(label)


# --------------------------------------------------------------------------- #
# 1. 解析器契约：复刻 hermes 源码的精确渲染
# --------------------------------------------------------------------------- #
def render_profile_list(profiles, active):
    out = [f"\n {'Profile':<16} {'Model':<28} {'Gateway':<12} {'Alias':<12} {'Distribution'}",
           f" {'─'*15}    {'─'*27}    {'─'*11}    {'─'*11}    {'─'*20}"]
    for p in profiles:
        marker = " ◆" if (p['name'] == active or (active == 'default' and p['is_default'])) else "  "
        model = (p['model'] or '—')[:26]
        gw = 'running' if p['gw'] else 'stopped'
        alias = '—' if p['is_default'] else (p['name'] if p['alias'] else '—')
        dist = (p['dist'] or '—')[:30]
        out.append(f"{marker}{p['name']:<15} {model:<28} {gw:<12} {alias:<12} {dist}")
    out.append("")
    return "\n".join(out)


sample = [
    {'name': 'default', 'model': 'deepseek-v4-pro', 'gw': False, 'alias': False, 'dist': None, 'is_default': True},
    {'name': 'art-design', 'model': 'deepseek/deepseek-chat', 'gw': True, 'alias': True, 'dist': None, 'is_default': False},
    {'name': 'coding', 'model': 'deepseek/deepseek-chat', 'gw': False, 'alias': False, 'dist': 'pack@1.2', 'is_default': False},
]
rows = server.parse_profile_list(render_profile_list(sample, 'art-design'))
by = {r['name']: r for r in rows} if rows else {}
check("profile list 解析出 3 行", rows is not None and len(rows) == 3)
check("art-design 标记 active", by.get('art-design', {}).get('active') is True)
check("default 未误判 active", by.get('default', {}).get('active') is False)
check("model 解析", by.get('art-design', {}).get('model') == 'deepseek/deepseek-chat')
check("gateway running", by.get('art-design', {}).get('gateway') == 'running')
check("alias '—'→None", by.get('coding', {}).get('alias') is None)
check("distribution 解析", by.get('coding', {}).get('distribution') == 'pack@1.2')

# 真实终端的新会话必须把 dashboard 软继承模型传给原生 Hermes；resume 保留会话自身路由。
_orig_effective_model_entry = server._effective_model_entry
try:
    server._effective_model_entry = lambda _name: {
        "default": "gpt-5.6-sol",
        "provider": "openai-codex",
    }
    _fresh_terminal = server._terminal_chat_command("personal-website")
    _resume_terminal = server._terminal_chat_command("personal-website", "20260719_test")
    check("真实终端新会话带软继承 model", "--model gpt-5.6-sol" in _fresh_terminal)
    check("真实终端新会话带软继承 provider", "--provider openai-codex" in _fresh_terminal)
    check("真实终端 resume 保留会话路由", "--resume 20260719_test" in _resume_terminal and "--provider" not in _resume_terminal)
finally:
    server._effective_model_entry = _orig_effective_model_entry


def render_sessions_with_titles(items):
    out = [f"{'Title':<32} {'Preview':<40} {'Last Active':<13} {'ID'}", "─" * 110]
    for s in items:
        out.append(f"{s['title'][:30]:<32} {s['preview'][:38]:<40} {s['la']:<13} {s['id']}")
    return "\n".join(out)


def render_sessions_no_titles(items):
    out = [f"{'Preview':<50} {'Last Active':<13} {'Src':<6} {'ID'}", "─" * 95]
    for s in items:
        out.append(f"{s['preview'][:48]:<50} {s['la']:<13} {s['src']:<6} {s['id']}")
    return "\n".join(out)


ps1 = server.parse_sessions(render_sessions_with_titles([
    {'title': '设计评审 讨论', 'preview': '看一下这个 logo 的比例问题', 'la': '2 hours ago', 'id': 'abc123'},
]))
check("sessions(有标题) 解析", len(ps1) == 1 and ps1[0]['title'] == '设计评审 讨论')
check("last_active 含空格保留", ps1 and ps1[0]['last_active'] == '2 hours ago')
ps2 = server.parse_sessions(render_sessions_no_titles([
    {'preview': '帮我查报关单据', 'la': '5 minutes ago', 'src': 'cli', 'id': 'zzz999'},
]))
check("sessions(无标题) 解析", len(ps2) == 1 and ps2[0].get('src') == 'cli')
check("空会话返回 []", server.parse_sessions("No sessions found.") == [])

# --------------------------------------------------------------------------- #
# 2. 真实文件系统：当前组织树 / 技能继承
# --------------------------------------------------------------------------- #
# 当前技能共享走 skills.external_dirs，不再依赖旧 design-core symlink 拓扑。
check("art-design 当前为普通组织节点", server.agent_role('art-design') == 'none')
check("fashion 当前为普通组织节点", server.agent_role('fashion') == 'none')
check("coding=none(独立)", server.agent_role('coding') == 'none')
check("fashion 挂在 art-design 下", server.effective_parent("fashion") == "art-design")

# --------------------------------------------------------------------------- #
# 3. FastAPI 端到端 + 校准诊断
# --------------------------------------------------------------------------- #
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)
data = client.get("/api/profiles").json()
names = {p['name'] for p in data['profiles']}
roles = {p['name']: p['role'] for p in data['profiles']}
print(f"\n[诊断] 数据源 = {data['source']}  |  hermes 可执行 = {shutil.which('hermes') or server.HERMES_BIN}")
print(f"[诊断] 发现 profile = {sorted(names)}")

check("/api/profiles 含 art-design", 'art-design' in names)
check("art-design 角色=none", roles.get('art-design') == 'none')
check("fashion 角色=none", roles.get('fashion') == 'none')

# 校准信号：本机有 hermes，但解析没命中 → 退回 filesystem
if shutil.which("hermes") and data['source'] != 'cli':
    warns.append("本机有 hermes 但数据源是 filesystem —— parse_profile_list 未命中真实输出，"
                 "请贴 `hermes profile list` 的真实输出对齐列宽/表头")

d = client.get("/api/profile/art-design").json()
check("detail 含 SOUL", len(d['soul']) > 0)
check("detail skills 非空", len(d['skills']) > 0)
check("detail sessions 是列表", isinstance(d['sessions'], list))

# 关键：继承方应通过 external_dirs 看到祖先技能，并标注继承来源。
# 技能分发后来源不一定是 default：如 Art&Design 自有设计技能会被 fashion 继承←art-design。
fa = client.get("/api/profile/fashion").json()
check("fashion 继承技能非空", len(fa['skills']) > 0)
ask = client.get("/api/agent-skills/fashion").json()
check("agent-skills 标注技能继承来源",
      bool(ask['skills']) and all(s['inherited'] and s.get('source') in {'default', 'art-design'} for s in ask['skills']))

dev = client.get("/api/profile/coding").json()
check("dev 也能看到 X 的共享技能", len(dev['skills']) > 0)
check("非法 profile 名被拒", client.get("/api/profile/../etc").status_code in (400, 404))

# 4b. /ws/chat —— PTY 终端通路（设 HERMES_CHAT_CMD=cat 做回显烟测）
if os.environ.get("HERMES_CHAT_CMD"):
    try:
        with client.websocket_connect("/ws/chat/art-design") as wsk:
            wsk.send_text(json.dumps({"type": "input", "data": "echo_ping\n"}))
            got = b""
            for _ in range(8):
                msg = wsk.receive()
                if msg.get("bytes"):
                    got += msg["bytes"]
                elif msg.get("text"):
                    got += msg["text"].encode()
                if b"echo_ping" in got:
                    break
            check("WS/PTY 回显通路 (cat)", b"echo_ping" in got)
    except Exception as e:  # noqa: BLE001
        check("WS/PTY 回显通路 (cat)", False)
        print("  WS 异常:", e)
else:
    warns.append("未设 HERMES_CHAT_CMD，跳过 WS/PTY 烟测（真机直接连真 hermes chat 验证）")
check("旧 /api/chat 已移除(404/405)",
      client.post("/api/chat", json={"profile": "art-design", "message": "x"}).status_code in (404, 405))

# --------------------------------------------------------------------------- #
# 5. 继承合并语义（dict 深合并 / list 并集 / 幂等）—— 修正"整段覆盖抹掉子级自有项"
# --------------------------------------------------------------------------- #
m = server._merge_inherited_value
check("dict 并集(子级自有键保留)", m({"a": 1}, {"b": 2}) == {"a": 1, "b": 2})
check("dict 父级覆盖同名叶子", m({"a": 1}, {"a": 9, "b": 2}) == {"a": 9, "b": 2})
check("list 并集", m([1, 2], [2, 3]) == [1, 2, 3])
check("list 合并幂等(轮询安全)", m(m([1, 2], [2, 3]), [2, 3]) == [1, 2, 3])
check("标量父级 wins", m("x", "y") == "y")
check("子级缺键则取父级整体", m(None, {"a": 1}) == {"a": 1})

# --------------------------------------------------------------------------- #
# 6. 规范体检 linter（hyphen-case + 必备 frontmatter；只读）
# --------------------------------------------------------------------------- #
ln = server._lint_name
check("lint: a-b 合规(无违规)", ln("skill", "apple-notes") == [])
check("lint: a_b 软违规(建议连字符)", ln("skill", "apple_notes")[:1] and ln("skill", "apple_notes")[0][0] == "soft")
check("lint: 大写/空格 硬违规", ln("skill", "Bad Name")[:1] and ln("skill", "Bad Name")[0][0] == "hard")
lp = server.lint_profile("art-design")
check("lint_profile 返回结构完整", all(k in lp for k in ("hard", "soft", "ok", "hard_count")))
check("art-design 自有 skill 无硬违规", lp["hard_count"] == 0)
lr = client.get("/api/lint/art-design").json()
check("/api/lint/{name} 可用", lr.get("name") == "art-design" and "hard" in lr)
lall = client.get("/api/lint").json()
check("/api/lint 全网汇总", "profiles" in lall and "total_hard" in lall)
check("非法 profile lint 被拒", client.get("/api/lint/../etc").status_code in (400, 404))

# --------------------------------------------------------------------------- #
# 7. Kill switch（级联停用 + 端点 net-zero）
# --------------------------------------------------------------------------- #
ek = server.effective_killed
check("kill 级联：父停用→子冻结", ek("fashion", {"art-design"}) is True)
check("kill 不波及无关 agent", ek("coding", {"art-design"}) is False)
check("kill 空集=全员启用", ek("fashion", set()) is False)
# 端点往返（net-zero：停用→查→恢复→查）
r1 = client.post("/api/kill", json={"name": "art-design", "killed": True}).json()
p_killed = client.get("/api/profile/fashion").json()
r2 = client.post("/api/kill", json={"name": "art-design", "killed": False}).json()
p_back = client.get("/api/profile/fashion").json()
check("kill 端点：停用生效 + 级联到子", r1.get("killed") is True and p_killed.get("effective_killed") is True)
check("kill 端点：恢复后清零(net-zero)", r2.get("killed") is False and p_back.get("effective_killed") is False)
check("kill 非法名被拒", client.post("/api/kill", json={"name": "../etc", "killed": True}).status_code in (400, 404))

# --------------------------------------------------------------------------- #
# 8. Constitution（受管块注入/剥离，纯函数幂等 + 还原）
# --------------------------------------------------------------------------- #
inj, strip = server._inject_constitution, server._strip_constitution
_soul = "You are Hermes Agent.\nHelpful and direct.\n"
_c = "## 红线\n不得未确认大额支出"
_a = inj(_soul, _c)
check("宪法块注入到顶部", _a.startswith(server._CONST_BEGIN))
check("宪法注入保留原人格", "You are Hermes Agent." in _a)
check("宪法注入幂等(再注入不变)", inj(_a, _c) == _a)
check("剥离宪法块=还原原文", strip(_a) == _soul)
check("空宪法=只剥不加", inj(_a, "") == _soul)
cg = client.get("/api/constitution").json()
check("/api/constitution GET 可用", cg.get("root") == "default" and isinstance(cg.get("nodes"), list))

# --------------------------------------------------------------------------- #
# 9. default 上位（主 agent = 树根；散建 agent 兜底归 default；组织子节点跟随 hierarchy）
# --------------------------------------------------------------------------- #
check("default 是树根(无父)", server.effective_parent("default") is None)
check("散建 agent 兜底归 default", server.effective_parent("business-builder") == "default")
check("fashion 仍归 art-design", server.effective_parent("fashion") == "art-design")
_t = client.get("/api/network").json()
check("/api/network 根只有 default", _t["roots"] == ["default"])
check("各 CEO 挂在 default 下",
      {"business-builder", "art-design", "coding"} <= set(_t["nodes"]["default"]["children"]))
check("art-design 仍带设计师子树",
      {"fashion", "fine-art", "ui-ux"} <= set(_t["nodes"]["art-design"]["children"]))

# --------------------------------------------------------------------------- #
# 10. 双分身 architect/steward（结构判定 + 挂在 default 下 + external_dirs 继承技能）
# --------------------------------------------------------------------------- #
if (server._profile_dir("architect").is_dir() and server._profile_dir("steward").is_dir()):
    check("architect 是分身(memories symlink→default)", server.is_main_twin("architect") is True)
    check("steward 是分身", server.is_main_twin("steward") is True)
    check("art-design 不是分身", server.is_main_twin("art-design") is False)
    check("default 不是自己的分身", server.is_main_twin("default") is False)
    check("分身挂在 default 下", server.effective_parent("architect") == "default")
    check("分身共享 default 全部技能(symlink)",
          server.count_skills("architect") == server.count_skills("default") > 0)
    check("分身共享 default 整份 config(含 mcp_servers，symlink 非快照)",
          server._read_config("architect") == server._read_config("default")
          and "mcp_servers" in server._read_config("architect"))
    _ti = client.get("/api/profile/architect").json()
    check("/api/profile 暴露 main_twin", _ti.get("main_twin") is True)
else:
    warns.append("architect/steward 分身 profile 不存在，跳过双分身校验")

# --------------------------------------------------------------------------- #
# 11. config 沿组织树继承（子可覆盖；幂等；根/分身跳过）—— 只读校验当前状态
# --------------------------------------------------------------------------- #
_eff = server._effective_config("art-design")
check("沿树继承：art-design 有效 config 含 default 的 mcp_servers",
      _eff.get("mcp_servers") == server._read_config("default").get("mcp_servers")
      and bool(_eff.get("mcp_servers")) is not False)
check("model 软继承：不进入真实 config 继承键，但能沿父链解析有效模型",
      "model" not in server._INHERITABLE_KEYS and bool(server.read_effective_config_model("art-design")[0]))
check("幂等：再物化一次无改动", server._materialize_tree(dry_run=True) == [])
_root_track = server._profile_dir("default") / server._INHERIT_TRACK
check("根跳过：default 无继承账本内容",
      not _root_track.exists() or server._load_inherit_track("default") == {})
if server._profile_dir("architect").is_dir():
    check("分身跳过：architect config.yaml 仍是 symlink",
          (server._profile_dir("architect") / "config.yaml").is_symlink())
_ct = client.get("/api/config-tree?dry_run=true").json()
check("/api/config-tree 可用且已收敛(无 changes)",
      "changes" in _ct and _ct["changes"] == [])

# --------------------------------------------------------------------------- #
# 12. 去冗余 / 转继承（只读 dry-run + external_dirs 门控；不改盘）
# --------------------------------------------------------------------------- #
_dd = server._convert_to_inherit("coding", dry_run=True)
check("转继承 dry-run 已收敛或识别冗余副本",
      isinstance(_dd.get("redundant", []), list))
check("转继承 dry-run 不动盘(removed 为空)", _dd.get("removed") == [])
check("转继承会从 default 继承", any("/.hermes/skills" in d for d in _dd.get("ancestors_inherited_from", [])))
check("主 agent/分身 不参与转继承", bool(server._convert_to_inherit("default", dry_run=True).get("error")))
check("convert-inherit 预览端点可用",
      client.get("/api/skills/convert-inherit/coding").json().get("name") == "coding")
check("门控：未转继承的 profile 不带祖先 external_dirs",
      not any("/.hermes/skills" == e for e in
              ((server._read_config("coding").get("skills") or {}).get("external_dirs") or [])))

# --------------------------------------------------------------------------- #
# 13. 落位预演 /api/reparent-preview（拖动确认前的全 profile diff，纯计算不写盘）
# --------------------------------------------------------------------------- #
import hashlib
import time

def _file_fingerprint(p):
    if not os.path.isfile(p):
        return None
    return (os.path.getmtime(p), hashlib.sha1(open(p, "rb").read()).hexdigest())

_hier_path = str(server.HIER_FILE) if hasattr(server, "HIER_FILE") else os.path.join(os.path.dirname(server.__file__), "hierarchy.json")
_hier_before = _file_fingerprint(_hier_path)
_devcfg_path = str(server._profile_dir("coding") / "config.yaml")
_devcfg_before = _file_fingerprint(_devcfg_path)

# 纯函数：祖先链 / 祖先 skills 目录 在假定层级 h 下计算正确
_h_real = server._load_hierarchy()
_h_sim = dict(_h_real); _h_sim["coding"] = "art-design"
_chain = server._ancestor_chain("coding", _h_sim)
check("ancestor_chain 假定层级正确(coding → art-design → default)",
      _chain == ["art-design", "default"])
_dirs = server._ancestor_skill_dirs_h("coding", _h_sim)
check("ancestor_skill_dirs_h 输出祖先 skills 目录",
      all(d.endswith("/skills") for d in _dirs) and len(_dirs) == 2)

# 端点：基础 diff（coding → art-design），结构完整
_pr = client.post("/api/reparent-preview", json={"node": "coding", "new_parent": "art-design"}).json()
check("/api/reparent-preview 顶层键齐全",
      all(k in _pr for k in ("node", "from", "to", "config_diff", "skill_diff", "constitution", "is_main_twin", "warnings", "descendants")))
check("preview from.parent = default(当前)", _pr["from"]["parent"] == "default")
check("preview to.parent = art-design(假定)", _pr["to"]["parent"] == "art-design")
check("preview to.ancestors 包含 art-design→default",
      _pr["to"]["ancestors"] == ["art-design", "default"])
check("preview 列出 inheritable_keys", bool(_pr["config_diff"]["inheritable_keys"]))

# 净零：预演不应改任何文件
_hier_after = _file_fingerprint(_hier_path)
_devcfg_after = _file_fingerprint(_devcfg_path)
check("预演净零：hierarchy.json 未被改写", _hier_before == _hier_after)
check("预演净零：coding/config.yaml 未被改写", _devcfg_before == _devcfg_after)

# 边界：自环 / 环引用 / 不存在父级 / 非法名
check("自环被拒(400)",
      client.post("/api/reparent-preview", json={"node": "coding", "new_parent": "coding"}).status_code == 400)
check("环引用被拒(409: default 在 art-design 的子树之外但 art-design 是 default 的后代)",
      client.post("/api/reparent-preview", json={"node": "default", "new_parent": "art-design"}).status_code == 409)
check("new_parent 不存在(404)",
      client.post("/api/reparent-preview", json={"node": "coding", "new_parent": "no-such-agent"}).status_code == 404)
check("非法 node 名(400)",
      client.post("/api/reparent-preview", json={"node": "../etc", "new_parent": "default"}).status_code == 400)

# 顶层化（new_parent=None）也能预演
_pr_top = client.post("/api/reparent-preview", json={"node": "fashion", "new_parent": None}).json()
check("顶层化预演：to.parent=None",
      _pr_top.get("to", {}).get("parent") is None)
check("顶层化：to.ancestors 为空",
      _pr_top.get("to", {}).get("ancestors") == [])

# 分身（架构师/管家）：is_main_twin=true + warning + 空 config_diff
if (server._profile_dir("architect").is_dir()):
    _pr_tw = client.post("/api/reparent-preview", json={"node": "architect", "new_parent": "art-design"}).json()
    check("分身预演：is_main_twin=true", _pr_tw.get("is_main_twin") is True)
    check("分身预演：config_diff.items 为空(symlink 镜像，不走树继承)",
          _pr_tw.get("config_diff", {}).get("items") == [])
    check("分身预演：含告警",
          any("分身" in w for w in _pr_tw.get("warnings", [])))

# 后代计数（移动 art-design 会带 3 个设计师子节点跟随）
_pr_dc = client.post("/api/reparent-preview", json={"node": "art-design", "new_parent": "coding"}).json()
check("后代预演：descendants 非空",
      len(_pr_dc.get("descendants", [])) >= 3)
check("后代预演：含跟随告警",
      any("后代" in w for w in _pr_dc.get("warnings", [])))

# 门控：未转继承的 profile，ext_dirs_before/after 永远为空（无 .no-bundled-skills）
_marker = server._profile_dir("coding") / ".no-bundled-skills"
if not _marker.exists():
    check("门控：未转继承 → preview ext_dirs_before=[]",
          _pr["skill_diff"]["ext_dirs_before"] == [])
    check("门控：未转继承 → preview ext_dirs_after=[]",
          _pr["skill_diff"]["ext_dirs_after"] == [])
    check("门控：未转继承 → preview skill_inherit_on=false",
          _pr["skill_diff"]["skill_inherit_on"] is False)

# --------------------------------------------------------------------------- #
# 14. config 继承单键 opt-out（block 存储 + materialize 尊重 + 净零回滚）
# --------------------------------------------------------------------------- #
# 选 art-design 做测试：它有 default 继承下来的 mcp_servers / fallback_providers 等。
# 测试场景：往 block 加 fallback_providers → materialize 不再写它 → 清 block → materialize 回来。

_test_target = "art-design"
_block_path = str(server.CONFIG_BLOCK_FILE)
_track_path = str(server._profile_dir(_test_target) / server._INHERIT_TRACK)
_cfgp = str(server._profile_dir(_test_target) / "config.yaml")
# 备份原始状态
_block_backup = open(_block_path, "rb").read() if os.path.isfile(_block_path) else None
_track_backup = open(_track_path, "rb").read() if os.path.isfile(_track_path) else None
_cfg_backup = open(_cfgp, "rb").read()

try:
    # 选一个肯定被继承的键（不在自有里）。从 effective_inherited 计算。
    _eff_now = server._effective_config(_test_target)
    _own_now = server._own_config(_test_target)
    _candidates = [k for k in server._INHERITABLE_KEYS if k in _eff_now and k not in _own_now]
    if not _candidates:
        warns.append("art-design 没有可测试 block 的继承键，跳过 §14 部分检查")
        _testkey = None
    else:
        _testkey = _candidates[0]
        check(f"找到可测试的继承键 '{_testkey}'", _testkey is not None)

        # 加入 block
        ball = server._load_config_block_all()
        ball.setdefault(_test_target, [])
        if _testkey not in ball[_test_target]:
            ball[_test_target] = sorted(set(ball[_test_target]) | {_testkey})
        server._save_config_block_all(ball)
        check("写入 block 列表生效", _testkey in server._load_config_block(_test_target))

        # 清掉该 key 的 track 让它"变成自有"（不被覆盖）
        tr = server._load_inherit_track(_test_target)
        tr.pop(_testkey, None)
        server._save_inherit_track(_test_target, tr)

        # 物化：不应该再把该键加进来
        server._materialize_tree(dry_run=False)
        _cfg_after_block = server._read_config(_test_target)
        _own_after_block = server._own_config(_test_target)
        _eff_after_block_for_self = {k: v for k, v in _cfg_after_block.items()}  # _materialize 写盘的结果
        check("block 生效：materialize 不再注入该键（或保留为自有，不被父辈覆盖）",
              _testkey not in server._load_inherit_track(_test_target))

        # 关键：block 不影响"作为父级被孙辈看到"——fashion 应该仍能看到这个键
        _fd_eff = server._effective_config("fashion")
        check(f"block 局部化：fashion 仍能看到 {_testkey}（通过 art-design 的 eff 透传到子辈）",
              _testkey in _fd_eff)

        # 清 block，回退
        ball2 = server._load_config_block_all()
        if _test_target in ball2:
            blk = set(ball2[_test_target])
            blk.discard(_testkey)
            if blk:
                ball2[_test_target] = sorted(blk)
            else:
                ball2.pop(_test_target, None)
            server._save_config_block_all(ball2)
        server._materialize_tree(dry_run=False)
        check("清除 block 后 materialize 重新接受继承",
              _testkey not in server._load_config_block(_test_target))

    # /api/move skill_inherit_off 切换：测试 round-trip（取个不在 off 集合里的 profile）
    _off_path = str(server.SKILL_INHERIT_OFF_FILE)
    _off_backup = open(_off_path, "rb").read() if os.path.isfile(_off_path) else None
    _was_off = "coding" in server._load_skill_inherit_off()
    # 模拟弹窗里调用：先 off=true 再 off=false
    server._save_skill_inherit_off(server._load_skill_inherit_off() | {"coding"})
    check("skill_inherit_off 加入生效", "coding" in server._load_skill_inherit_off())
    s2 = server._load_skill_inherit_off()
    s2.discard("coding")
    server._save_skill_inherit_off(s2)
    check("skill_inherit_off 移除生效", "coding" not in server._load_skill_inherit_off())
    # 还原 off 状态
    if _was_off:
        server._save_skill_inherit_off(server._load_skill_inherit_off() | {"coding"})
    if _off_backup is not None:
        open(_off_path, "wb").write(_off_backup)

finally:
    # 净零：把所有可能被改动的文件原样还原
    if _block_backup is not None:
        open(_block_path, "wb").write(_block_backup)
    elif os.path.isfile(_block_path):
        os.remove(_block_path)
    if _track_backup is not None:
        open(_track_path, "wb").write(_track_backup)
    elif os.path.isfile(_track_path):
        os.remove(_track_path)
    open(_cfgp, "wb").write(_cfg_backup)
    # 再物化一次让内存/磁盘一致
    server._materialize_tree(dry_run=False)

# 净零验收
_block_after = open(_block_path, "rb").read() if os.path.isfile(_block_path) else None
check("§14 净零：config_inherit_block.json 还原", _block_after == _block_backup)
check("§14 净零：art-design/config.yaml 还原",
      open(_cfgp, "rb").read() == _cfg_backup)

# --------------------------------------------------------------------------- #
# 15. 草稿态：/api/agent/create-empty → drafts.json → 落位清除标记（端到端净零）
# --------------------------------------------------------------------------- #
_draft_name = "test-draft-zz"
_drafts_path = str(server.DRAFTS_FILE)
_hier_path2 = str(server.HIERARCHY_FILE)
_drafts_bak = open(_drafts_path, "rb").read() if os.path.isfile(_drafts_path) else None
_hier_bak2 = open(_hier_path2, "rb").read() if os.path.isfile(_hier_path2) else None

try:
    # 1) 创建空 agent
    cr = client.post("/api/agent/create-empty", json={"name": _draft_name, "description": "测试草稿"})
    check("create-empty 端点可用", cr.status_code == 200 and cr.json().get("is_draft") is True)
    check("hermes profile 目录已生成", server._profile_dir(_draft_name).is_dir())
    check("草稿登记进 drafts.json", _draft_name in server._load_drafts())

    # 2) effective_parent 显式 None（不挂 default）
    check("草稿 effective_parent=None（不兜底归 default）",
          server.effective_parent(_draft_name) is None)

    # 3) /api/network 把它放在 drafts 字段，不出现在 nodes/roots
    _net = client.get("/api/network").json()
    check("草稿不进 nodes", _draft_name not in _net["nodes"])
    check("草稿不在 roots", _draft_name not in _net["roots"])
    check("草稿出现在 drafts 字段", any(d["name"] == _draft_name for d in _net.get("drafts", [])))

    # 4) /api/profiles 标记 is_draft=true
    _ps = client.get("/api/profiles").json()
    _dt = next((p for p in _ps["profiles"] if p["name"] == _draft_name), None)
    check("profiles 含 is_draft=true 标记", _dt is not None and _dt.get("is_draft") is True)

    # 5) 重名拒绝
    cr2 = client.post("/api/agent/create-empty", json={"name": _draft_name})
    check("重名草稿被拒(409)", cr2.status_code == 409)

    # 6) 非法名拒绝
    cr3 = client.post("/api/agent/create-empty", json={"name": "Bad Name"})
    check("非法草稿名被拒(400)", cr3.status_code == 400)

    # 7) /api/reparent-preview 对草稿：from.parent=None, to.ancestors 从 np 起算（不被 draft 锁死）
    _pv = client.post("/api/reparent-preview", json={"node": _draft_name, "new_parent": "art-design"}).json()
    check("草稿 preview: from.parent=None", _pv["from"]["parent"] is None)
    check("草稿 preview: to.ancestors=[art-design, default]",
          _pv["to"]["ancestors"] == ["art-design", "default"])

    # 8) 落位 → 草稿标记清除 + parent=art-design
    mv = client.post("/api/move", json={"node": _draft_name, "new_parent": "art-design"}).json()
    check("/api/move 返回 was_draft=true", mv.get("was_draft") is True)
    check("落位后 drafts 不再含该草稿", _draft_name not in server._load_drafts())
    check("落位后 effective_parent=art-design",
          server.effective_parent(_draft_name) == "art-design")
    _net2 = client.get("/api/network").json()
    check("落位后出现在 art-design 的 children",
          _draft_name in _net2["nodes"].get("art-design", {}).get("children", []))
    check("落位后不再在 drafts 字段",
          not any(d["name"] == _draft_name for d in _net2.get("drafts", [])))

finally:
    # 净零：删 profile + 还原文件
    import subprocess
    subprocess.run(["hermes", "profile", "delete", _draft_name, "-y"],
                   capture_output=True, text=True, timeout=20)
    if _drafts_bak is not None:
        open(_drafts_path, "wb").write(_drafts_bak)
    elif os.path.isfile(_drafts_path):
        os.remove(_drafts_path)
    if _hier_bak2 is not None:
        open(_hier_path2, "wb").write(_hier_bak2)
    elif os.path.isfile(_hier_path2):
        os.remove(_hier_path2)

# 净零验收
_drafts_after = open(_drafts_path, "rb").read() if os.path.isfile(_drafts_path) else None
_hier_after2 = open(_hier_path2, "rb").read() if os.path.isfile(_hier_path2) else None
check("§15 净零：drafts.json 还原", _drafts_after == _drafts_bak)
check("§15 净零：hierarchy.json 还原", _hier_after2 == _hier_bak2)
check("§15 净零：test profile 已删", not server._profile_dir(_draft_name).is_dir())

# --------------------------------------------------------------------------- #
# 16. 仪表盘 summary 聚合（3 张卡：系统健康 / 待你 Review / 最近变更）—— 只读 + 净零
# --------------------------------------------------------------------------- #
_dash_before_files = {
    str(server.HIERARCHY_FILE): (open(server.HIERARCHY_FILE, "rb").read() if server.HIERARCHY_FILE.is_file() else None),
    str(server.DRAFTS_FILE): (open(server.DRAFTS_FILE, "rb").read() if server.DRAFTS_FILE.is_file() else None),
}
_ds = client.get("/api/dashboard/summary").json()
check("/api/dashboard/summary 顶层键齐全",
      all(k in _ds for k in ("health", "review", "recent_changes", "generated_at")))
# health
_h = _ds["health"]
check("health 含 agent_total/main/twin/sub/drafts",
      all(k in _h for k in ("agent_total", "main", "twin", "sub", "drafts")))
check("health.agent_total > 0", _h["agent_total"] > 0)
check("health.main == 1（default 是唯一主 agent）", _h["main"] == 1)
check("health 计数自洽：main + twin + sub == agent_total",
      _h["main"] + _h["twin"] + _h["sub"] == _h["agent_total"])
check("health 含 lint 与 mcp 统计",
      all(k in _h for k in ("lint_hard_total", "lint_soft_total", "default_mcp_count", "agents_with_mcp", "const_subscribed")))
# review
_rv = _ds["review"]
check("review 含 killed/hard_lint/drafts", all(k in _rv for k in ("killed", "hard_lint", "drafts")))
check("review.hard_lint 每条结构正确",
      all(("profile" in x and "items" in x and "count" in x) for x in _rv["hard_lint"]))
check("review.hard_lint 计数自洽（每项 count == items[:5]+ 其余）",
      all(x["count"] >= len(x["items"]) for x in _rv["hard_lint"]))
# recent_changes
_rc = _ds["recent_changes"]
check("recent_changes 是列表", isinstance(_rc, list))
check("recent_changes 限 ≤20 条", len(_rc) <= 20)
check("recent_changes 时间倒序",
      all(_rc[i]["ago_seconds"] <= _rc[i + 1]["ago_seconds"] for i in range(len(_rc) - 1)))
check("recent_changes 每条结构正确",
      all(all(k in c for k in ("kind", "target", "summary", "when", "ago", "ago_seconds")) for c in _rc))
# 调用是只读的：再调一次，hierarchy/drafts.json 不应被改动
_ds2 = client.get("/api/dashboard/summary").json()
check("/api/dashboard/summary 只读：再调一次仍 200", isinstance(_ds2, dict) and "health" in _ds2)
_dash_after_files = {
    str(server.HIERARCHY_FILE): (open(server.HIERARCHY_FILE, "rb").read() if server.HIERARCHY_FILE.is_file() else None),
    str(server.DRAFTS_FILE): (open(server.DRAFTS_FILE, "rb").read() if server.DRAFTS_FILE.is_file() else None),
}
check("§16 净零：hierarchy.json 未改", _dash_before_files[str(server.HIERARCHY_FILE)] == _dash_after_files[str(server.HIERARCHY_FILE)])
check("§16 净零：drafts.json 未改", _dash_before_files[str(server.DRAFTS_FILE)] == _dash_after_files[str(server.DRAFTS_FILE)])

# --------------------------------------------------------------------------- #
# 17. Vault（全局知识库）：文件夹树 + 读写 + 路径安全 + 净零
#     关键：用一个临时库（monkeypatch _vault_root），绝不碰用户真实的 llmwiki 库。
# --------------------------------------------------------------------------- #
import tempfile as _tempfile
_vault_orig_root = server._vault_root
_vault_tmp = server.Path(_tempfile.mkdtemp(prefix="hermes_vault_test_"))
server._vault_root = lambda: _vault_tmp

try:
    # 1) 空库：exists=true, file_count=0, tree 空
    _v = client.get("/api/vault").json()
    check("空库 exists=true file_count=0 tree=[]",
          _v["exists"] is True and _v["file_count"] == 0 and _v["tree"] == [])

    # 2) 写笔记（自动建中间目录）
    _w1 = client.post("/api/vault/note", json={"path": "raw/inbox/t.md", "content": "# hi\n[[link]]"})
    check("写笔记 200 overwritten=false", _w1.status_code == 200 and _w1.json().get("overwritten") is False)
    check("中间目录被自动创建", (_vault_tmp / "raw" / "inbox" / "t.md").is_file())

    # 3) 树里能看到它（dir 在前、file_count 对）
    _v2 = client.get("/api/vault").json()
    _top = {n["name"]: n for n in _v2["tree"]}
    check("树 file_count=1 且顶层有 raw 目录",
          _v2["file_count"] == 1 and _top.get("raw", {}).get("type") == "dir")

    # 4) 读回内容（含 path + abs_path）
    _g = client.get("/api/vault/note", params={"path": "raw/inbox/t.md"}).json()
    check("读回内容正确 + 带 abs_path",
          "hi" in _g["content"] and _g["path"] == "raw/inbox/t.md" and "abs_path" in _g)

    # 5) 读不存在 → 404
    check("读不存在 404", client.get("/api/vault/note", params={"path": "nope.md"}).status_code == 404)

    # 6) 路径安全：遍历 / 非 .md / 隐藏目录 / 备份文件 全拒(400)
    check("路径遍历被拒(400)",
          client.post("/api/vault/note", json={"path": "../../etc/passwd.md", "content": "x"}).status_code == 400)
    check("非 .md 被拒(400)",
          client.post("/api/vault/note", json={"path": "x.txt", "content": "x"}).status_code == 400)
    check("隐藏目录被拒(400)",
          client.post("/api/vault/note", json={"path": ".obsidian/c.md", "content": "x"}).status_code == 400)
    check("备份文件名被拒(400)",
          client.post("/api/vault/note", json={"path": "x.md.dashbak", "content": "x"}).status_code == 400)

    # 7) 覆盖写：.dashbak 备份生成
    _w2 = client.post("/api/vault/note", json={"path": "raw/inbox/t.md", "content": "# updated"})
    check("覆盖写 overwritten=true", _w2.json().get("overwritten") is True)
    check(".dashbak 备份生成", (_vault_tmp / "raw" / "inbox" / "t.md.dashbak").is_file())

    # 8) 隐藏目录 / .dashbak 不进树、不计入 file_count（即便文件系统里有）
    (_vault_tmp / ".obsidian").mkdir(exist_ok=True)
    (_vault_tmp / ".obsidian" / "app.md").write_text("cfg", encoding="utf-8")
    _v3 = client.get("/api/vault").json()
    check(".obsidian 不进树 + .dashbak 不计数",
          ".obsidian" not in [n["name"] for n in _v3["tree"]] and _v3["file_count"] == 1)

finally:
    server._vault_root = _vault_orig_root
    shutil.rmtree(_vault_tmp, ignore_errors=True)

# 净零验收：真实库未被碰（root 函数已还原 + 临时库已删）
check("§17 净零：_vault_root 已还原", server._vault_root is _vault_orig_root)
check("§17 净零：临时库已删", not _vault_tmp.exists())

# --------------------------------------------------------------------------- #
# 18. Profile 面板扩展端点：impact / backups / restore / subtasks
# --------------------------------------------------------------------------- #
# impact for art-design —— 应包含 3 个设计师后代
_im = client.get("/api/profile/art-design/impact").json()
check("/api/profile/{name}/impact 结构完整",
      all(k in _im for k in ("name", "descendants", "config_inherit", "external_dirs",
                              "symlink_consumers", "summary")))
check("art-design impact: descendants = 3 设计师",
      set(_im["descendants"]) == {"fashion", "fine-art", "ui-ux"})
check("art-design impact.summary.descendants_count == 3", _im["summary"]["descendants_count"] == 3)
check("art-design impact.external_dirs 列出设计师们",
      set(_im["external_dirs"]) == {"fashion", "fine-art", "ui-ux"})

# 叶子节点 impact = 空
_im_leaf = client.get("/api/profile/fashion/impact").json()
check("叶子节点 impact: descendants 为空", _im_leaf["descendants"] == [])

# 非法名 / 不存在
check("impact 非法名 400",
      client.get("/api/profile/Bad.Name/impact").status_code == 400)
check("impact 不存在 404",
      client.get("/api/profile/no-such-profile/impact").status_code == 404)

# backups for art-design —— 当前可以为空，但结构必须稳定
_bk = client.get("/api/profile/art-design/backups").json()
check("/api/profile/{name}/backups 结构完整", "backups" in _bk and isinstance(_bk["backups"], list))
check("art-design backups 条目结构可解析",
      all("file" in b and "kind" in b for b in _bk["backups"]))
check("backups 时间倒序",
      all(_bk["backups"][i]["mtime"] >= _bk["backups"][i+1]["mtime"]
          for i in range(len(_bk["backups"]) - 1)))
check("每条 backup 含 kind/file/ago/when/restore_target",
      all(all(k in b for k in ("kind", "file", "ago", "when", "restore_target")) for b in _bk["backups"]))

# restore round-trip：备份内容写回 → pre-restore 自动留底 → 再恢复回原状（净零）
_devcfg_path2 = str(server._profile_dir("coding") / "config.yaml")
_devcfg_orig = open(_devcfg_path2, "rb").read()
_devbak_path = str(server._profile_dir("coding") / "config.yaml.dashbak")
_devbak_orig = open(_devbak_path, "rb").read() if os.path.isfile(_devbak_path) else None
try:
    if _devbak_orig is not None:
        _rs = client.post("/api/profile/coding/restore", json={"file": "config.yaml.dashbak"}).json()
        check("/api/profile/{name}/restore 成功", _rs.get("ok") is True)
        check("restore 自动生成 .pre-restore 备份",
              os.path.isfile(_devcfg_path2 + ".pre-restore"))
        # 还原 config.yaml 到测试前状态 + 删 pre-restore
        open(_devcfg_path2, "wb").write(_devcfg_orig)
        pre_path = _devcfg_path2 + ".pre-restore"
        if os.path.isfile(pre_path):
            os.remove(pre_path)
    else:
        warns.append("coding 无 .dashbak，跳过 restore round-trip")
finally:
    # 双保险：确认 config.yaml 没被改
    pass
check("§18 净零：coding/config.yaml 还原",
      open(_devcfg_path2, "rb").read() == _devcfg_orig)
check("§18 净零：.pre-restore 已清理",
      not os.path.isfile(_devcfg_path2 + ".pre-restore"))

# 非法 restore：备份文件不存在
check("restore 不存在的备份(404)",
      client.post("/api/profile/coding/restore", json={"file": "no-such.dashbak"}).status_code == 404)

# subtasks
_st = client.get("/api/profile/art-design/subtasks").json()
check("/api/profile/{name}/subtasks 结构正确",
      "tasks" in _st and "total" in _st and isinstance(_st["tasks"], list))

# --------------------------------------------------------------------------- #
# 19. Vault MCP server stdio 协议 + 与仪表盘往返
# --------------------------------------------------------------------------- #
import subprocess as _sp

def _mcp(req_obj):
    """跑一次 vault MCP server（全局库，不再需要 profile），输入一条 JSON-RPC 请求，返回响应 dict。"""
    env = dict(os.environ); env["HERMES_DASHBOARD_URL"] = "http://127.0.0.1:8877"
    p = _sp.run(["python3", os.path.join(os.path.dirname(server.__file__), "mcp_servers", "hermes_vault_mcp.py")],
                input=json.dumps(req_obj), capture_output=True, text=True, env=env, timeout=10)
    out = p.stdout.strip()
    return json.loads(out) if out else None

# 注意：这一段依赖 dashboard server 已经在 8877 跑，才能往返
import urllib.request as _urlreq
try:
    _urlreq.urlopen("http://127.0.0.1:8877/api/health", timeout=2)
    _dash_up = True
except Exception:  # noqa: BLE001
    _dash_up = False

if not _dash_up:
    warns.append("dashboard server 未在 8877 运行，跳过 §19 MCP server 往返测试")
else:
    # initialize
    _r = _mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    check("MCP initialize 返回 protocolVersion",
          _r and _r.get("result", {}).get("protocolVersion") == "2024-11-05")
    check("MCP initialize 返回 serverInfo",
          _r and _r.get("result", {}).get("serverInfo", {}).get("name") == "hermes-vault")
    # tools/list
    _r = _mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    _tool_names = sorted([t["name"] for t in _r["result"]["tools"]])
    check("MCP tools/list 包含 3 个工具",
          _tool_names == ["vault_list", "vault_read", "vault_write"])
    # ping
    _r = _mcp({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}})
    check("MCP ping 返回 result={}", _r.get("result") == {})
    # 未知方法
    _r = _mcp({"jsonrpc": "2.0", "id": 4, "method": "no_such_method", "params": {}})
    check("MCP 未知方法返回 error -32601",
          _r.get("error", {}).get("code") == -32601)

    # 往返（只读，天然净零）：vault_list 命中全局库，证明 MCP 子进程 → 仪表盘 → 新全局端点贯通。
    # 不在自动化里 vault_write —— 真实库是用户的 llmwiki，写入不可净零。写路径已在 §17 用临时库覆盖。
    _l = _mcp({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
               "params": {"name": "vault_list", "arguments": {}}})
    _ltxt = _l["result"]["content"][0]["text"]
    check("MCP vault_list 贯通全局知识库端点",
          (not _l["result"].get("isError")) and "全局知识库" in _ltxt)

# --------------------------------------------------------------------------- #
# 20. Dashboard MCP server（组织树感知 + 治理工具）—— stdio 协议 + 往返（只读，天然净零）
# --------------------------------------------------------------------------- #
def _dash_mcp(req_obj, profile="art-design"):
    env = dict(os.environ); env["HERMES_PROFILE"] = profile; env["HERMES_DASHBOARD_URL"] = "http://127.0.0.1:8877"
    p = _sp.run(["python3", os.path.join(os.path.dirname(server.__file__), "mcp_servers", "hermes_dashboard_mcp.py")],
                input=json.dumps(req_obj), capture_output=True, text=True, env=env, timeout=10)
    out = p.stdout.strip()
    return json.loads(out) if out else None

if not _dash_up:
    warns.append("dashboard server 未在 8877 运行，跳过 §20 dashboard MCP 测试")
else:
    _r = _dash_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    _names = sorted(t["name"] for t in _r["result"]["tools"])
    check("dashboard MCP 含 6 个工具",
          _names == ["agent_detail", "agent_impact", "dashboard_summary", "lint_check", "my_position", "org_tree"])
    # my_position(art-design) 应列出 3 个设计师后代
    _r = _dash_mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "my_position", "arguments": {}}}, profile="art-design")
    _txt = _r["result"]["content"][0]["text"]
    check("my_position 列出 art-design 的直接下属",
          all(n in _txt for n in ("fashion", "fine-art", "ui-ux"))
          and "parent: default" in _txt)
    # org_tree 含树根 default + 分身标记
    _r = _dash_mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "org_tree", "arguments": {}}})
    _txt = _r["result"]["content"][0]["text"]
    check("org_tree 含 default 根 + 分身标记",
          "default" in _txt and "分身" in _txt)
    # agent_detail 缺参数 → isError
    _r = _dash_mcp({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "agent_detail", "arguments": {}}})
    check("dashboard MCP agent_detail 缺参数报错",
          _r["result"].get("isError") is True)
    # 未知方法 → -32601
    _r = _dash_mcp({"jsonrpc": "2.0", "id": 5, "method": "no_such", "params": {}})
    check("dashboard MCP 未知方法返回 -32601",
          _r.get("error", {}).get("code") == -32601)
    # 草稿 agent 的 my_position：友好提示而非崩
    _r = _dash_mcp({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "my_position", "arguments": {}}}, profile="no-such-zzz")
    check("dashboard MCP 未知 profile my_position 不崩",
          _r["result"].get("isError") is True or "不在组织树" in _r["result"]["content"][0]["text"])

# --------------------------------------------------------------------------- #
# 21. 显示名 / pin / rename / fork / delete + delegate MCP
# --------------------------------------------------------------------------- #
# 显示名层
_lbl = client.get("/api/profiles").json()
_by = {p["name"]: p for p in _lbl["profiles"]}
check("profiles 带 label 字段", all("label" in p for p in _lbl["profiles"]))
check("default 显示名 = X", _by.get("default", {}).get("label") == "X")
check("art-design 显示名 = Art&Design", _by.get("art-design", {}).get("label") == "Art&Design")
check("profiles 带 is_pinned 字段", all("is_pinned" in p for p in _lbl["profiles"]))

# pin round-trip（net-zero）
_pin_before = open(str(server.PINNED_FILE), "rb").read() if server.PINNED_FILE.is_file() else None
try:
    r1 = client.post("/api/agent/coding/pin", json={"pinned": True}).json()
    check("pin 生效", r1.get("pinned") is True and "coding" in r1.get("pinned_list", []))
    check("pin 反映到 profiles", [p for p in client.get("/api/profiles").json()["profiles"] if p["name"] == "coding"][0]["is_pinned"] is True)
    r2 = client.post("/api/agent/coding/pin", json={"pinned": False}).json()
    check("unpin 生效", r2.get("pinned") is False)
finally:
    if _pin_before is not None:
        open(str(server.PINNED_FILE), "wb").write(_pin_before)
    elif server.PINNED_FILE.is_file():
        server.PINNED_FILE.unlink()
check("§21 pin 净零", (open(str(server.PINNED_FILE), "rb").read() if server.PINNED_FILE.is_file() else None) == _pin_before)

# rename 显示名 round-trip（net-zero）
_lbl_before = open(str(server.LABELS_FILE), "rb").read()
_dev_label_before = server._label("coding")
try:
    client.post("/api/agent/coding/rename", json={"display": "开发测试ZZ"})
    check("rename 改显示名生效", server._label("coding") == "开发测试ZZ")
finally:
    open(str(server.LABELS_FILE), "wb").write(_lbl_before)
check("§21 rename 净零", server._label("coding") == _dev_label_before)

# delete 防护
check("delete default 被拒(400)", client.delete("/api/agent/default").status_code == 400)
check("delete 有子节点的 art-design 被拒(409)", client.delete("/api/agent/art-design").status_code == 409)
check("delete 不存在的 404", client.delete("/api/agent/no-such-zz").status_code == 404)

# fork + delete 闭环（net-zero：建副本→验证→删掉）
_hier_b = open(str(server.HIERARCHY_FILE), "rb").read()
_fork_id = "law-fork-zz"
try:
    fr = client.post("/api/agent/law/fork", json={"new_name": _fork_id, "new_display": "法律副本ZZ"})
    if fr.status_code == 200:
        fj = fr.json()
        check("fork 成功，落在源同一父级", fj.get("parent") == server.effective_parent("law"))
        check("fork profile 目录已建", server._profile_dir(_fork_id).is_dir())
        check("fork 显示名生效", server._label(_fork_id) == "法律副本ZZ")
        # 删掉副本
        dr = client.delete(f"/api/agent/{_fork_id}")
        check("删除 fork 副本成功", dr.status_code == 200)
        check("fork 副本目录已删", not server._profile_dir(_fork_id).is_dir())
        check("删除后 hierarchy 不含副本", _fork_id not in server._load_hierarchy())
    else:
        warns.append(f"fork 返回 {fr.status_code}（可能 hermes --clone-from 不可用），跳过 fork 闭环")
finally:
    # 双保险清理
    import subprocess as _sp2
    if server._profile_dir(_fork_id).is_dir():
        _sp2.run(["hermes", "profile", "delete", _fork_id, "-y"], capture_output=True, timeout=20)
    _h = server._load_hierarchy()
    if _fork_id in _h:
        _h.pop(_fork_id); server._save_hierarchy(_h)
    _lb = server._load_labels()
    if _fork_id in _lb:
        _lb.pop(_fork_id); server._save_labels(_lb)
check("§21 fork 净零：hierarchy 还原", open(str(server.HIERARCHY_FILE), "rb").read() == _hier_b)

# delegate MCP（直调）stdio 协议 + 防护
if _dash_up:
    def _del_mcp(req_obj, profile="event"):
        env = dict(os.environ); env["HERMES_PROFILE"] = profile
        p = _sp.run(["python3", os.path.join(os.path.dirname(server.__file__), "mcp_servers", "hermes_delegate_mcp.py")],
                    input=json.dumps(req_obj), capture_output=True, text=True, env=env, timeout=10)
        return json.loads(p.stdout.strip()) if p.stdout.strip() else None
    _r = _del_mcp({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    check("delegate MCP 暴露 call_agent", [t["name"] for t in _r["result"]["tools"]] == ["call_agent"])
    _r = _del_mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "call_agent", "arguments": {"target": "event", "prompt": "hi"}}}, profile="event")
    check("delegate 防自调", _r["result"].get("isError") is True and "自己" in _r["result"]["content"][0]["text"])
    _r = _del_mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "call_agent", "arguments": {"target": "Bad Name", "prompt": "x"}}})
    check("delegate 拒非法 target", _r["result"].get("isError") is True)

# --------------------------------------------------------------------------- #
# 22. 运行设置 4 杠杆（model/memory/terminal/approvals）—— GET 结构 + 分身拒 + set/revert 净零
# --------------------------------------------------------------------------- #
_lv = client.get("/api/agent/trading/levers").json()
check("levers GET 结构完整",
      all(k in _lv for k in ("model", "model_own", "memory_enabled", "memory_own",
                             "terminal_backend", "terminal_own", "approvals_mode", "approvals_own", "known_models")))
check("levers 未设时 terminal 默认 local 且非自有", _lv["terminal_backend"] == "local" and _lv["terminal_own"] is False)
check("levers 未设时 approvals 默认 manual", _lv["approvals_mode"] == "manual")
check("known_models 非空（至少 default 的模型）", len(_lv["known_models"]) >= 1)
check("known_models 按模型名唯一",
      len({m["default"] for m in _lv["known_models"]}) == len(_lv["known_models"]))
check("levers 返回推理强度",
      _lv["reasoning_effort"] in {"none", "minimal", "low", "medium", "high", "xhigh"})
check("分身 levers 编辑被拒(409)",
      client.post("/api/agent/architect/levers", json={"approvals_mode": "off"}).status_code == 409)
check("levers 拒非法终端后端(400)",
      client.post("/api/agent/trading/levers", json={"terminal_backend": "bad-backend"}).status_code == 400)

# set + revert 净零
_tcfg = str(server._profile_dir("trading") / "config.yaml")
_tcfg_before = open(_tcfg, "rb").read()
_tbak = str(server._profile_dir("trading") / "config.yaml.dashbak")
_tbak_existed = os.path.isfile(_tbak)
try:
    client.post("/api/agent/trading/levers",
                json={"terminal_backend": "docker", "approvals_mode": "smart", "memory_enabled": False})
    _r = client.get("/api/agent/trading/levers").json()
    check("levers set 生效：terminal=docker own", _r["terminal_backend"] == "docker" and _r["terminal_own"] is True)
    check("levers set 生效：approvals=smart", _r["approvals_mode"] == "smart")
    check("levers set 生效：memory=off own", _r["memory_enabled"] is False and _r["memory_own"] is True)
    # reasoning_effort（agent 子项，整键覆盖 + 回退）
    check("levers GET 含 reasoning_effort（默认 medium 非自有）",
          _lv.get("reasoning_effort") == "medium" and _lv.get("reasoning_own") is False)
    client.post("/api/agent/trading/levers", json={"reasoning_effort": "high"})
    _rr = client.get("/api/agent/trading/levers").json()
    check("reasoning set=high own", _rr["reasoning_effort"] == "high" and _rr["reasoning_own"] is True)
    check("reasoning 拒非法档(400)",
          client.post("/api/agent/trading/levers", json={"reasoning_effort": "ultra"}).status_code == 400)
    client.post("/api/agent/trading/levers", json={"reasoning_effort": "__inherit__"})
    _rr2 = client.get("/api/agent/trading/levers").json()
    check("reasoning __inherit__ 回退 medium 非自有", _rr2["reasoning_effort"] == "medium" and _rr2["reasoning_own"] is False)
    # 还原（继承）
    client.post("/api/agent/trading/levers",
                json={"terminal_backend": "__inherit__", "approvals_mode": "__inherit__", "memory_enabled": "__inherit__"})
    _r2 = client.get("/api/agent/trading/levers").json()
    check("levers __inherit__ 回退：terminal 回 local 非自有", _r2["terminal_own"] is False)
finally:
    open(_tcfg, "wb").write(_tcfg_before)
    if not _tbak_existed and os.path.isfile(_tbak):
        os.remove(_tbak)
check("§22 净零：trading config.yaml 还原", open(_tcfg, "rb").read() == _tcfg_before)

# --------------------------------------------------------------------------- #
# 23. 记忆面板：GET/POST MEMORY.md+USER.md + 跨 agent 搜（净零）
# --------------------------------------------------------------------------- #
_mem = client.get("/api/agent/default/memory").json()
check("记忆 GET 结构完整",
      all(k in _mem for k in ("memory", "memory_size", "user", "user_size", "is_twin")))
check("X 有沉淀的记忆（memory_size>0）", _mem["memory_size"] > 0)
# 跨 agent 搜
_ms = client.get("/api/memory/search", params={"q": "hermes"}).json()
check("跨 agent 搜记忆有命中", _ms["count"] > 0 and all(("agent" in h and "line" in h) for h in _ms["hits"]))
check("空查询返回空", client.get("/api/memory/search", params={"q": ""}).json()["hits"] == [])
# 写 + 还原（净零）—— 用一个当前空记忆的 agent
_tgt = "research-hub"
_memdir = server._memories_dir(_tgt)
_existed = _memdir.is_dir()
_mfile = _memdir / "MEMORY.md"
_mfile_existed = _mfile.is_file()
try:
    r = client.post(f"/api/agent/{_tgt}/memory", json={"memory": "# 测试ZZ\n内容"})
    check("记忆 POST 写入成功", r.status_code == 200 and "MEMORY.md" in r.json()["wrote"])
    check("写后读得到", client.get(f"/api/agent/{_tgt}/memory").json()["memory_size"] > 0)
finally:
    # 净零清理（仅清我们造的）
    if not _mfile_existed and _mfile.is_file():
        _mfile.unlink()
    _bak = _mfile.with_suffix(_mfile.suffix + ".membak")
    if _bak.is_file():
        _bak.unlink()
    if not _existed and _memdir.is_dir():
        try: _memdir.rmdir()
        except OSError: pass
check("§23 净零：测试记忆已清",
      (server._memories_dir(_tgt) / "MEMORY.md").is_file() == _mfile_existed)
check("记忆非法 profile 名 400", client.get("/api/agent/Bad.Name/memory").status_code == 400)

# --------------------------------------------------------------------------- #
print()
for w in warns:
    print("⚠️  校准提示:", w)
print("\n" + ("ALL PASS ✅" if not fails else f"{len(fails)} FAILED ❌: {fails}"))
sys.exit(1 if fails else 0)
