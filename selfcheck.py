#!/usr/bin/env python3.11
"""更新后自检 (post-`hermes update` smoke test).

纯确定性断言,不需要 AI:逐项验证 dashboard 与 Hermes 内核的耦合点,绿/红 + 退出码。
检测=脚本(本文件);修复=按需把红项交给 Claude Code 或 steward/dev agent。

用法:
    python3.11 ~/.hermes/dashboard/selfcheck.py            # 离线快检
退出码 0=全绿,1=有红项。
"""
import os, sys, json, tempfile, time
from pathlib import Path
# 钉死 HERMES_HOME 到本脚本所在的真实 hermes 根(dashboard 的上级目录)。
# 否则被某个 profile agent(如 dev,其 HERMES_HOME=profiles/dev)调用时,server.py 会把
# PROFILES_DIR 算成 <profile>/profiles/,读不到真正的子 profile → 误报红。
# selfcheck 检的是「dashboard ↔ 真实根」的耦合,永远以真实根为准,与谁来调用无关。
os.environ["HERMES_HOME"] = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402  复用 dashboard 自己的解析/继承逻辑——它们正是要被验的耦合点

results = []


def check(name):
    def deco(fn):
        try:
            ok, detail = fn()
        except Exception as e:  # 任何异常都算红,绝不让单项炸掉整次体检
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append((name, ok, detail))
        return fn
    return deco


@check("CLI 输出解析 (parse_profile_list)")
def _c1():
    rc, out, err = server.run_hermes(["profile", "list"], timeout=30)
    if rc != 0:
        return False, f"`hermes profile list` rc={rc}: {(err or out)[:80]}"
    rows = server.parse_profile_list(out)
    if not rows:
        return False, "解析得到 0 行 —— CLI 表格格式可能变了 → 修 parse_profile_list"
    if "default" not in json.dumps(rows, ensure_ascii=False):
        return False, f"{len(rows)} 行但找不到 default,字段可能错位"
    return True, f"{len(rows)} 个 profile,解析正常"


@check("config schema / 继承键")
def _c2():
    keys = server._INHERITABLE_KEYS
    if not isinstance(keys, (list, tuple)) or len(keys) < 10 or "model" in keys:
        return False, f"_INHERITABLE_KEYS 异常: {keys}"
    x = server._read_config("default")
    if not isinstance(x, dict) or "model" not in x or "mcp_servers" not in x:
        return False, "X(default) config 缺核心键 model/mcp_servers —— schema 变了?"
    return True, f"X config 正常,真实继承键 {len(keys)} 个；model 为软继承"


@check("继承物化幂等 (art-design, 干跑)")
def _c3():
    server._materialize_config("art-design", {}, dry_run=True)   # dry_run 不写盘,净零
    eff = server._effective_config("art-design").get("mcp_servers") or {}
    if not eff:
        return False, "art-design 有效 mcp 为空 —— 物化/继承断了"
    return True, f"art-design 有效 mcp {len(eff)} 项,物化无异常"


@check("组织树 (build_tree)")
def _c4():
    t = server.build_tree()
    blob = json.dumps(t, ensure_ascii=False, default=str)
    if '"default"' not in blob:
        return False, "树里没有 default 根 —— build_tree 断了"
    nodes = t.get("nodes", t) if isinstance(t, dict) else {}
    n = len(nodes) if isinstance(nodes, dict) else 0
    return True, f"树正常,约 {n} 节点"


@check("SOUL 宪法注入块完整")
def _c6():
    beg = getattr(server, "_CONST_BEGIN", None)
    end = getattr(server, "_CONST_END", None)
    if not beg or not end:
        return True, "(无宪法注入常量,跳过)"
    bad = []
    for name in server._all_profile_names():
        s = server._profile_dir(name) / "SOUL.md"
        if not s.exists():
            continue
        txt = s.read_text(encoding="utf-8", errors="ignore")
        if txt.count(beg) != txt.count(end):
            bad.append(name)
    if bad:
        return False, f"SOUL 注入块不配对(可能被破坏): {bad}"
    return True, "所有 SOUL 注入块配对完整"


@check("宪法 rule_id 覆盖/屏蔽语义")
def _c_const_rules():
    old = {
        "CONSTITUTION_FILE": server.CONSTITUTION_FILE,
        "_profile_dir": server._profile_dir,
        "_all_profile_names": server._all_profile_names,
        "_load_labels": server._load_labels,
        "effective_parent": server.effective_parent,
        "build_tree": server.build_tree,
        "is_main_twin": server.is_main_twin,
        "_last_const_sig": getattr(server, "_last_const_sig", None),
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profiles = root / "profiles"
            profiles.mkdir()
            for name in ["coding", "leaf"]:
                d = profiles / name
                d.mkdir()
                (d / "SOUL.md").write_text(f"{name} persona\n", encoding="utf-8")
            (root / "SOUL.md").write_text("default persona\n", encoding="utf-8")
            (root / "constitution.md").write_text("### [rule:family-love] 家庭偏好\n我爱妈妈\n", encoding="utf-8")
            (profiles / "coding" / "constitution.md").write_text("", encoding="utf-8")
            (profiles / "leaf" / "constitution.md").write_text(
                "### [override:family-love] 家庭偏好\n我爱爸爸\n",
                encoding="utf-8",
            )

            server.CONSTITUTION_FILE = root / "constitution.md"
            server._profile_dir = lambda name: root if name == "default" else profiles / name
            server._all_profile_names = lambda: ["default", "coding", "leaf"]
            server._load_labels = lambda: {}
            server.effective_parent = lambda name: {"leaf": "coding", "coding": "default"}.get(name)
            server.build_tree = lambda: {"nodes": {"default": {"children": ["coding"]}, "coding": {"children": ["leaf"]}, "leaf": {"children": []}}}
            server.is_main_twin = lambda _name: False
            server._last_const_sig = None

            server._constitution_sync_tick(force=True)
            leaf = (profiles / "leaf" / "SOUL.md").read_text(encoding="utf-8")
            if "我爱爸爸" not in leaf or "我爱妈妈" in leaf:
                return False, "子级 override 未遮住父级同 rule_id"

            time.sleep(0.01)
            (root / "constitution.md").write_text("### [rule:family-love] 家庭偏好\n我爱爷爷\n", encoding="utf-8")
            server._constitution_sync_tick()
            leaf = (profiles / "leaf" / "SOUL.md").read_text(encoding="utf-8")
            if "我爱爸爸" not in leaf or "我爱爷爷" in leaf:
                return False, "父级更新覆盖了子级 override"

            time.sleep(0.01)
            (profiles / "leaf" / "constitution.md").write_text("### [disable:family-love]\n", encoding="utf-8")
            server._constitution_sync_tick()
            leaf = (profiles / "leaf" / "SOUL.md").read_text(encoding="utf-8")
            if "我爱爸爸" in leaf or "我爱爷爷" in leaf:
                return False, "子级 disable 未屏蔽父级同 rule_id"
        return True, "override/disable 按显式 rule_id 生效,且 watcher 可检测直接文件改动"
    finally:
        server.CONSTITUTION_FILE = old["CONSTITUTION_FILE"]
        server._profile_dir = old["_profile_dir"]
        server._all_profile_names = old["_all_profile_names"]
        server._load_labels = old["_load_labels"]
        server.effective_parent = old["effective_parent"]
        server.build_tree = old["build_tree"]
        server.is_main_twin = old["is_main_twin"]
        server._last_const_sig = old["_last_const_sig"]


@check("默认继承收敛 (无未剥离的祖先重复副本)")
def _c_converge():
    # 默认继承门控已是"默认开"：每个非分身子 agent 都从祖先吃 external_dirs 继承。hermes update 会把
    # 内置技能重新 bundle 进各 profile,与祖先逐字节相同的物理副本回潮、与继承双计。本检 dry-run 扫全树,
    # 确认无 agent 仍持有这类重复副本。红 → 跑 POST /api/skills/converge-all 重新剥离(可逆,移备份)。
    pend = server._converge_inheritance(dry_run=True)     # dry_run 只预览,不动盘,净零
    if pend:
        names = ", ".join(f"{p['name']}({p['stripped']})" for p in pend)
        return False, f"{len(pend)} 个 agent 有未剥离的祖先重复副本: {names} → 跑 converge-all"
    return True, "全树已收敛,无祖先重复物理副本(默认继承生效)"


@check("curator 安全不变式 (prune_builtins 全树=false)")
def _c_curator_safety():
    # hermes 某次升级把 curator.prune_builtins 默认 False→True,导致 curator 把出厂(bundled)技能
    # 当"低价值"自动归档(2026-06 一次裁了 25 个)。dashboard 用 _enforce_curator_safety 把它强制为
    # false(开机 + ~60s tick),并沿组织树继承到全部 agent。本检确认不变式真的全树生效。
    # 红 → 跑 POST /api/curator/safety,或确认 _enforce_curator_safety 仍在 _config_sync_loop 里。
    bad = []
    for name in server._all_profile_names():
        eff = server._effective_config(name).get("curator") or {}
        if eff.get("prune_builtins") is not False:   # None(未设→hermes 默认 True)或 True 都算红
            bad.append(f"{name}={eff.get('prune_builtins', '(unset)')}")
    if bad:
        return False, f"{len(bad)} 个 agent 的 prune_builtins 非 false: {', '.join(bad[:8])} → 跑 /api/curator/safety"
    return True, "全树 curator.prune_builtins=false(出厂技能不会被 curator 裁)"


@check("上下文索引 INDEX.md (index_builder)")
def _c_index():
    # 四项:①受管块可渲染 ②落盘文件块标记完整 ③手写覆盖区未丢
    #      ④漂移检测(INDEX 里的 agent 集 == 磁盘 profile 集)——这是"_index_sync_tick 是否还活着"
    #        的确定性信号:不靠脆弱的时间阈值,靠"结构变了 INDEX 有没有跟上"。
    # 红 → 跑 POST /api/index/rebuild 或 python3 dashboard/index_builder.py 重建。
    import re as _re
    import index_builder
    blk = index_builder.build_managed_block(server.HERMES_HOME)
    if index_builder.MANAGED_BEGIN not in blk or "## Agent 名录" not in blk:
        return False, "受管块渲染异常 —— index_builder 与 profiles 结构可能脱钩"
    p = server.HERMES_HOME / "INDEX.md"
    if not p.is_file():
        return False, "INDEX.md 不存在 → 跑 POST /api/index/rebuild"
    txt = p.read_text(encoding="utf-8", errors="ignore")
    if index_builder.MANAGED_END not in txt:
        return False, "INDEX.md 受管块结束标记缺失(可能被手改破坏)"
    managed, tail = txt.split(index_builder.MANAGED_END, 1)
    if not tail.strip():
        return False, "手写覆盖区为空 —— 路由表丢失"
    # ④ 漂移:落盘 INDEX 提到的 agent id 集,应等于磁盘上真实 profile 集。
    #    不等 = 有 agent 增删改后 INDEX 没重建(tick 死了/落后),或 INDEX 被手改破坏。
    ids_in_index = set(_re.findall(r"([a-z0-9_-]+)（", managed))
    disk = set(index_builder._all_profiles(server.HERMES_HOME))
    missing = disk - ids_in_index          # 磁盘有、INDEX 无 → 新增没跟上
    stale = ids_in_index - disk            # INDEX 有、磁盘无 → 删除没跟上
    if missing or stale:
        parts = []
        if missing:
            parts.append(f"缺失{sorted(missing)[:6]}")
        if stale:
            parts.append(f"残留{sorted(stale)[:6]}")
        return False, f"INDEX 与磁盘 profile 漂移({';'.join(parts)}) → tick 可能未运行,跑 /api/index/rebuild"
    # 附加(不判红):手写路由表(仅表格行)里引用的 agent 是否都还存在。
    # 只扫 | 分隔的表格单元,按 、/空白切词,排除管道名与占位符,避免误报散文。
    note = ""
    try:
        _KW = {"call_agent", "vault_read", "vault", "target", "prompt"}
        cited = set()
        for row in tail.splitlines():
            if row.count("|") < 3:
                continue
            for cell in row.split("|"):
                for tok in _re.split(r"[、,/\s]+", cell.strip()):
                    if _re.fullmatch(r"[a-z][a-z0-9_-]{2,}", tok) and tok not in _KW:
                        cited.add(tok)
        gone = sorted(c for c in cited if c not in disk)
        if gone:
            note = f" · 注:路由表引用了不存在的 agent {gone[:5]}(手写区,请人工核对)"
    except Exception:
        pass
    return True, f"INDEX.md 正常({len(txt)} 字符),名录/磁盘一致({len(disk)} 个),覆盖区完整{note}"


@check("hermes doctor")
def _c7():
    rc, out, err = server.run_hermes(["doctor"], timeout=90)
    return (rc == 0), (f"doctor rc={rc}" + ("" if rc == 0 else f": {(err or out)[:80]}"))


def main():
    print("\n更新后自检 (post-`hermes update`)\n" + "=" * 54)
    rc, ver, _ = server.run_hermes(["--version"], timeout=15)
    print("Hermes 版本:", (ver or "?").strip().splitlines()[0] if ver else "?")
    print("-" * 54)
    allok = True
    for name, ok, detail in results:
        print(f"{'✅' if ok else '❌'} {name}")
        print(f"     {detail}")
        allok = allok and ok
    print("=" * 54)
    print("结论:", "全绿,放心 ✅" if allok else "有红项 ❌ —— 把红项贴给 Claude Code 修对接")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
