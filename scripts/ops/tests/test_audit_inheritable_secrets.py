# -*- coding: utf-8 -*-
"""audit_inheritable_secrets.py 的 fixture 测试。

全部在临时目录里造假 profile，绝不碰任何真实 ~/.hermes。
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import subprocess
import sys

import pytest

OPS_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = OPS_DIR.parent.parent
SCRIPT = OPS_DIR / "audit_inheritable_secrets.py"

sys.path.insert(0, str(OPS_DIR))
import audit_inheritable_secrets as A  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def fake_home(tmp_path):
    return A.build_fixture(tmp_path / "hermes")


@pytest.fixture(params=["auto", "builtin"])
def parser_mode(request, monkeypatch):
    monkeypatch.setattr(A, "FORCE_PARSER", request.param)
    return request.param


# --------------------------------------------------------------------------- #
# 1. 与 server.py 的 _INHERITABLE_KEYS 保持一致
# --------------------------------------------------------------------------- #

def _server_py_keys():
    server = REPO_ROOT / "server.py"
    if not server.is_file():
        return None
    text = server.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^_INHERITABLE_KEYS\s*=\s*(\[.*?\])", text, re.S | re.M)
    if not m:
        return None
    return ast.literal_eval(m.group(1))


def test_key_count_is_16():
    assert len(A.INHERITABLE_KEYS) == 16
    assert len(set(A.INHERITABLE_KEYS)) == 16


def test_keys_match_server_py():
    keys = _server_py_keys()
    if keys is None:
        pytest.skip("仓库里找不到 server.py 或它的 _INHERITABLE_KEYS 定义")
    assert keys == A.INHERITABLE_KEYS, (
        "scripts/ops 的继承键与 server.py 不一致 —— 必须原样同步")


# --------------------------------------------------------------------------- #
# 2. 分类器单测
# --------------------------------------------------------------------------- #

# Synthetic values: assemble the same detector inputs without storing token-
# shaped strings that repository push protection mistakes for credentials.
@pytest.mark.parametrize("prefix,suffix", [
    ("sk-ant-api03-", "ZmFrZUZBS0VmYWtlRkFLRTEyMzQ1Njc4OTA"),
    ("sk-proj-", "QWERTYuiop1234567890ZXCVBNMasdfgh"),
    ("ghp_", "abcdefGHIJKL0123456789mnopQRSTuvwx"),
    ("github_pat_", "11ABCDEFG0abcdefghijkl"),
    ("xoxb-", "123456789012-abcdefghijklmnop"),
    ("xapp-", "1-A012345-6789-abcdef"),
    ("AKIA", "IOSFODNN7EXAMPLE"),
    ("AIza", "SyA-abcdefghijklmnopqrstuvwx1234567"),
    ("glpat-", "abcdefgHIJKLMNOP1234"),
    ("hf_", "abcdefghijklmnopqrstuvwxyz"),
    ("-----BEGIN ", "RSA PRIVATE KEY-----"),
])
def test_credential_prefixes_detected(prefix, suffix):
    value = prefix + suffix
    cls, _reason = A.classify(("providers", "x", "value"), value)
    assert cls == A.CLS_PLAINTEXT, value


@pytest.mark.parametrize("value", [
    "${ANTHROPIC_API_KEY}",
    "$OPENAI_API_KEY",
    "env:MY_TOKEN",
    "%MY_TOKEN%",
    "{{ MY_TOKEN }}",
    "secret_ref:vault/anthropic",
])
def test_env_references_classified_as_reference(value):
    cls, _ = A.classify(("providers", "x", "api_key"), value)
    assert cls == A.CLS_REFERENCE, value


@pytest.mark.parametrize("value", [
    "claude-opus-4-20250514",
    "gpt-5",
    "api.anthropic.com",
    "https://api.anthropic.com/v1",
    "round_robin",
    "200000",
    "1.2.3",
    "true",
])
def test_benign_values_not_flagged(value):
    cls, _ = A.classify(("compression", "field"), value)
    assert cls in (A.CLS_BENIGN, A.CLS_EMPTY, A.CLS_PATH), (value, cls)


def test_numeric_value_under_suspicious_key_is_benign():
    # max_tokens 这类键名含 "token"，但值是数字 —— 不能报成凭据
    cls, reason = A.classify(("context", "max_tokens"), "1200000")
    assert cls == A.CLS_BENIGN and reason == "numeric"


def test_ancestor_key_name_does_not_taint_subtree():
    # credential_pool_strategies.rotation = round_robin 不应因为祖先键名被判成凭据
    cls, _ = A.classify(("credential_pool_strategies", "rotation"), "round_robin")
    assert cls == A.CLS_BENIGN
    assert A.key_name_suspicious(("credential_pool_strategies", "rotation")) is False
    assert A.key_name_suspicious(("providers", "anthropic", "api_key")) is True


def test_list_index_skipped_when_taking_leaf_key_name():
    assert A.leaf_key_name(("providers", "api_keys", "[0]")) == "api_keys"
    assert A.key_name_suspicious(("providers", "api_keys", "[0]")) is True


def test_path_value_is_path_ref_not_plaintext():
    cls, _ = A.classify(("mcp_servers", "local", "env", "TOKEN_FILE"), "~/.config/mcp/token.txt")
    assert cls == A.CLS_PATH


def test_high_entropy_token_detected():
    cls, reason = A.classify(("auxiliary", "blob"), "Zm9vYmFyLXNlY3JldC1LRVktOTk5OTk5OTk5OQ")
    assert cls == A.CLS_PLAINTEXT and reason == "high-entropy-token"


def test_redact_segment_hides_credential_shaped_mapping_key():
    assert A.redact_segment("anthropic") == "anthropic"
    assert A.redact_segment("sk-ant-api03-ZmFrZUZBS0Vm") == "<redacted-segment>"


# --------------------------------------------------------------------------- #
# 3. 内置 YAML 解析器
# --------------------------------------------------------------------------- #

def test_builtin_yaml_parser_matches_pyyaml_on_fixture(fake_home):
    yaml = pytest.importorskip("yaml")
    for name in ("default", "alpha", "beta", "gamma", "epsilon"):
        p = fake_home / "config.yaml" if name == "default" \
            else fake_home / "profiles" / name / "config.yaml"
        text = p.read_text(encoding="utf-8")
        want, _c = A.flatten_obj(yaml.safe_load(text))
        got, _gc, errors = A.flatten_yaml_text(text)
        assert errors == [], (name, errors)
        assert got == want, name


def test_builtin_yaml_parser_handles_shapes():
    text = (
        "mapping:\n"
        "  a: 1\n"
        "  b: 'quoted value'\n"
        "seq_same_indent:\n"
        "- one\n"
        "- two\n"
        "seq_indented:\n"
        "  - x\n"
        "  - y\n"
        "seq_of_maps:\n"
        "- name: first\n"
        "  cmd: /bin/true\n"
        "- name: second\n"
        "  cmd: /bin/false\n"
        "empty_map: {}\n"
        "empty_list: []\n"
        "block: |\n"
        "  line1\n"
        "  line2\n"
        "nothing:\n"
        "# comment line\n"
        "trailing: value  # inline comment\n"
    )
    leaves, containers, errors = A.flatten_yaml_text(text)
    assert errors == []
    assert leaves[("mapping", "a")] == "1"
    assert leaves[("mapping", "b")] == "quoted value"
    assert leaves[("seq_same_indent", "[0]")] == "one"
    assert leaves[("seq_same_indent", "[1]")] == "two"
    assert leaves[("seq_indented", "[0]")] == "x"
    assert leaves[("seq_of_maps", "[0]", "name")] == "first"
    assert leaves[("seq_of_maps", "[1]", "cmd")] == "/bin/false"
    assert leaves[("block",)] == "line1\nline2"
    assert leaves[("trailing",)] == "value"
    assert ("nothing",) in containers


def test_builtin_parser_matches_pyyaml_on_safe_dump_roundtrip():
    """config.yaml 是 `yaml.safe_dump(..., allow_unicode=True, sort_keys=False)` 写出来的，
       内置解析器必须在这类产物上与 PyYAML 拍平结果完全一致（含折行长标量、空容器）。"""
    yaml = pytest.importorskip("yaml")
    data = {
        "providers": {
            "anthropic": {"api_key": "sk-ant-XXXX", "base_url": "https://api.anthropic.com",
                          "timeout": 30, "retry": {"max": 3, "backoff": 1.5}},
            "openai": {"api_key": "${OPENAI_API_KEY}", "models": ["gpt-5", "gpt-4.1"]},
        },
        "mcp_servers": {"github": {"command": "npx",
                                   "args": ["-y", "@modelcontextprotocol/server-github"],
                                   "env": {"GITHUB_TOKEN": "ghp_x"}}},
        "hooks": {"pre_tool": [{"name": "a", "command": "/bin/true"},
                               {"name": "b", "command": "/bin/false"}]},
        "agent": {"reasoning_effort": "high", "enabled": True, "note": None,
                  "long_cjk": "这是一段很长的说明文字 " * 12,
                  "long_ascii": "the quick brown fox jumps over the lazy dog " * 4,
                  "apostrophe": "it's a test with 'quotes' inside and more text here"},
        "toolsets": ["read", "write", "exec"],
        "empty_map": {}, "empty_list": [],
        "compression": {"enabled": True, "trigger_tokens": 120000},
    }
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    want, _ = A.flatten_obj(yaml.safe_load(text))
    got, _c, errors = A.flatten_yaml_text(text)
    assert errors == []
    assert got == want


def test_builtin_yaml_parser_reports_tab_indent_as_problem():
    _l, _c, errors = A.flatten_yaml_text("providers:\n\t- broken\n")
    assert errors and any("TAB" in e for e in errors)


# --------------------------------------------------------------------------- #
# 4. 端到端审计
# --------------------------------------------------------------------------- #

def test_audit_counts(fake_home, parser_mode):
    res, _values = A.audit(fake_home)
    by_key = {r["key"]: r for r in res["by_key"]}

    assert res["profile_count"] == 6
    assert res["inheritable_key_count"] == 16
    assert set(res["profiles_scanned"]) == {
        "default", "alpha", "beta", "gamma", "delta", "epsilon"}

    # providers：default/alpha 明文，beta 只有引用
    assert {"default", "alpha", "beta"} <= set(by_key["providers"]["profiles_present"])
    assert set(by_key["providers"]["profiles_plaintext_suspect"]) == {"default", "alpha"}
    assert by_key["providers"]["profiles_reference"] == ["beta"]
    assert "beta" not in by_key["providers"]["profiles_plaintext_suspect"]

    # mcp_servers：alpha 明文 token，beta 只有路径
    assert by_key["mcp_servers"]["profiles_plaintext_suspect"] == ["alpha"]
    assert "beta" not in by_key["mcp_servers"]["profiles_plaintext_suspect"]

    # AWS key / 高熵串
    assert by_key["auxiliary"]["profiles_plaintext_suspect"] == ["gamma"]
    assert by_key["image_gen"]["profiles_plaintext_suspect"] == ["gamma"]

    # 祖先键名不再污染整棵子树
    assert by_key["credential_pool_strategies"]["profiles_plaintext_suspect"] == []

    # 完全没出现的键
    for absent in ("fallback_providers", "tool_loop_guardrails", "context",
                   "prompt_caching", "memory"):
        assert by_key[absent]["profiles_present"] == []


def test_materialization_ledger(fake_home, parser_mode):
    res, _ = A.audit(fake_home)
    by_key = {r["key"]: r for r in res["by_key"]}

    assert by_key["providers"]["profiles_materialized"] == ["alpha"]
    assert by_key["providers"]["profiles_materialized_unchanged"] == ["alpha"]
    assert by_key["providers"]["profiles_track_holds_plaintext_copy"] == ["alpha"]

    assert set(by_key["toolsets"]["profiles_materialized"]) == {"alpha", "beta"}
    assert by_key["toolsets"]["profiles_materialized_unchanged"] == ["alpha"]
    assert by_key["toolsets"]["profiles_materialized_changed"] == ["beta"]


def test_bad_yaml_is_reported_not_silently_clean(fake_home, parser_mode):
    res, _ = A.audit(fake_home)
    assert any(p["profile"] == "delta" for p in res["parse_problems"])


def test_twin_symlinked_config_is_flagged(tmp_path, parser_mode):
    """分身的 config.yaml 是指向根的 symlink：审计要能读到（那就是有效配置），
       同时在报告里标出 symlink，免得被当成独立的一份凭据。"""
    import os
    home = tmp_path / "hermes"
    (home / "profiles" / "twin").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "providers:\n  anthropic:\n    api_key: sk-ant-TWINFIXTURE0123456789\n", encoding="utf-8")
    os.symlink(str(home / "config.yaml"), str(home / "profiles" / "twin" / "config.yaml"))

    res, _ = A.audit(home)
    per = {p["profile"]: p for p in res["per_profile"]}
    assert per["twin"]["config_yaml_is_symlink"] is True
    assert per["default"]["config_yaml_is_symlink"] is False
    by_key = {r["key"]: r for r in res["by_key"]}
    assert set(by_key["providers"]["profiles_plaintext_suspect"]) == {"default", "twin"}
    assert "symlink" in A.render_markdown(res)


def test_profile_filter(fake_home, parser_mode):
    res, _ = A.audit(fake_home, only_profiles=["alpha"])
    assert res["profiles_scanned"] == ["alpha"]
    assert res["profile_count"] == 1


# --------------------------------------------------------------------------- #
# 5. 安全纪律：不读密钥文件、不输出值
# --------------------------------------------------------------------------- #

def test_only_config_and_track_files_are_read(fake_home, parser_mode, monkeypatch):
    opened = []
    real_read = pathlib.Path.read_text

    def spy(self, *a, **kw):
        opened.append(str(self))
        return real_read(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", spy)
    A.audit(fake_home)

    assert opened, "审计一个文件都没读，测试本身有问题"
    for path in opened:
        name = pathlib.Path(path).name
        assert name in ("config.yaml", A.INHERIT_TRACK), path
    joined = " ".join(opened)
    assert ".env" not in joined
    assert "credentials" not in joined
    assert "auth.json" not in joined


def test_report_contains_no_values(fake_home, parser_mode):
    res, values = A.audit(fake_home)
    md = A.render_markdown(res)
    js = json.dumps(res, ensure_ascii=False, indent=2)
    for secret in A.FIXTURE_SECRETS.values():
        assert secret not in md
        assert secret not in js
    # 通用防线：任何被扫描到的值都不许出现在输出里
    A.assert_no_values_leaked(md + js, values)


def test_leak_guard_actually_fires():
    with pytest.raises(A.ValueLeak):
        A.assert_no_values_leaked("报告里混进了 sk-ant-LEAKED-VALUE-1234",
                                  {"sk-ant-LEAKED-VALUE-1234": A.CLS_PLAINTEXT})
    # 纯字符串集合也接受（单测便利）
    with pytest.raises(A.ValueLeak):
        A.assert_no_values_leaked("报告里混进了 sk-ant-LEAKED-VALUE-1234",
                                  {"sk-ant-LEAKED-VALUE-1234"})


def test_leak_guard_error_message_has_class_and_length_but_no_value():
    with pytest.raises(A.ValueLeak) as exc:
        A.assert_no_values_leaked("泄漏了 sk-ant-LEAKED-VALUE-1234",
                                  {"sk-ant-LEAKED-VALUE-1234": A.CLS_PLAINTEXT})
    msg = str(exc.value)
    assert A.CLS_PLAINTEXT in msg
    assert str(len("sk-ant-LEAKED-VALUE-1234")) in msg
    assert "sk-ant-LEAKED-VALUE-1234" not in msg


# --------------------------------------------------------------------------- #
# 5b. 泄漏守卫的取值范围（回归：良性 7 字符值 `default` 触发的误报）
# --------------------------------------------------------------------------- #

def test_guard_set_excludes_benign_and_short_values():
    raw = [
        ("sk-ant-api03-LONGENOUGHSECRET0123", A.CLS_PLAINTEXT),  # 收
        ("default", A.CLS_BENIGN),                               # 良性
        ("deepseek", A.CLS_BENIGN),
        ("medium", A.CLS_BENIGN),
        ("AKIAIOSFODNN7EXAMPLE", A.CLS_PLAINTEXT),               # 收
        ("shortpw", A.CLS_PLAINTEXT),                            # 太短，不收
        ("/usr/local/bin/mcp-local-server", A.CLS_PATH),         # 路径，不收
        ("120000", A.CLS_BENIGN),                                # 数字，不收
        ("${ANTHROPIC_API_KEY}", A.CLS_REFERENCE),               # 只有占位符，残余为空
    ]
    guard = A.build_guard_set(raw, exclusion_terms=set())
    assert set(guard) == {"sk-ant-api03-LONGENOUGHSECRET0123", "AKIAIOSFODNN7EXAMPLE"}
    assert all(cls == A.CLS_PLAINTEXT for cls in guard.values())


def test_guard_set_excludes_values_equal_to_profile_names_or_path_segments():
    raw = [("credential_pool_strategies", A.CLS_PLAINTEXT),   # 恰好等于一个键路径分段
           ("my-agent-profile", A.CLS_PLAINTEXT),             # 恰好等于一个 profile 名
           ("sk-ant-api03-LONGENOUGHSECRET0123", A.CLS_PLAINTEXT)]
    guard = A.build_guard_set(raw, exclusion_terms={"credential_pool_strategies",
                                                    "my-agent-profile"})
    assert set(guard) == {"sk-ant-api03-LONGENOUGHSECRET0123"}


def test_guard_set_keeps_reference_residue():
    raw = [("${VAULT}/anthropic-production-key", A.CLS_REFERENCE),
           ("${ANTHROPIC_API_KEY}", A.CLS_REFERENCE),
           ("$SHORT", A.CLS_REFERENCE)]
    guard = A.build_guard_set(raw, exclusion_terms=set())
    assert set(guard) == {"/anthropic-production-key"}
    assert guard["/anthropic-production-key"] == A.CLS_REFERENCE


@pytest.mark.parametrize("value,expected", [
    ("${ANTHROPIC_API_KEY}", ""),
    ("$OPENAI_API_KEY", ""),
    ("%WIN_TOKEN%", ""),
    ("{{ MY_TOKEN }}", ""),
    ("${VAULT}/path/to/thing", "/path/to/thing"),
])
def test_reference_residue(value, expected):
    assert A.reference_residue(value) == expected


def test_benign_enum_value_equal_to_profile_name_does_not_trip_guard(tmp_path, parser_mode):
    """回归：profile 名叫 `default`，某个良性叶子的值也是 `default`。
       旧守卫（所有扫描值、min_len=6）会把报告里合法出现的 `default` 当成泄漏而中止。"""
    home = tmp_path / "hermes"
    (home / "profiles" / "beta").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "agent:\n"
        "  profile_name: default\n"          # 值 == profile 名（长度 7）
        "  provider_alias: deepseek\n"
        "  effort_label: medium\n"
        "compression:\n"
        "  strategy: default\n"
        "curator:\n"
        "  mode: providers\n",               # 值 == 一个继承键名
        encoding="utf-8")
    (home / "profiles" / "beta" / "config.yaml").write_text(
        "agent:\n  profile_name: default\n", encoding="utf-8")

    res, guard = A.audit(home)
    assert guard == {}, guard
    md = A.render_markdown(res)
    js = json.dumps(res, ensure_ascii=False, indent=2)
    assert "default" in md            # profile 名确实合法出现在报告里
    A.assert_no_values_leaked(md + js, guard)   # 不许抛

    r = _run("--hermes-home", str(home))
    assert r.returncode == 0, r.stderr + r.stdout
    assert r.stdout.startswith("# 继承键敏感值审计")
    r2 = _run("--hermes-home", str(home), "--md-out", str(tmp_path / "a.md"),
              "--json-out", str(tmp_path / "a.json"))
    assert r2.returncode == 0, r2.stderr + r2.stdout
    assert (tmp_path / "a.md").is_file() and (tmp_path / "a.json").is_file()


def test_report_mentions_key_paths(fake_home, parser_mode):
    res, _ = A.audit(fake_home)
    md = A.render_markdown(res)
    assert "providers.anthropic.api_key" in md
    assert "mcp_servers.github.env.GITHUB_TOKEN" in md
    assert "auxiliary.s3.access_key_id" in md


# --------------------------------------------------------------------------- #
# 6. CLI
# --------------------------------------------------------------------------- #

def _run(*args, **kw):
    return subprocess.run([sys.executable, str(SCRIPT)] + list(args),
                          capture_output=True, text=True, **kw)


def test_cli_self_test():
    for parser in ("auto", "builtin"):
        r = _run("--self-test", "--parser", parser)
        assert r.returncode == 0, r.stderr


def test_cli_markdown_and_json(fake_home, tmp_path):
    r = _run("--hermes-home", str(fake_home))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("# 继承键敏感值审计")

    r2 = _run("--hermes-home", str(fake_home), "--json")
    assert r2.returncode == 0, r2.stderr
    data = json.loads(r2.stdout)
    assert data["schema"] == "dashboard.inheritable_secret_audit.v1"
    assert len(data["by_key"]) == 16

    out = tmp_path / "audit.json"
    r3 = _run("--hermes-home", str(fake_home), "--json-out", str(out))
    assert r3.returncode == 0, r3.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["profile_count"] == 6
    assert r3.stdout.startswith("# 继承键敏感值审计")


def test_cli_fail_on_plaintext(fake_home, tmp_path):
    r = _run("--hermes-home", str(fake_home), "--fail-on-plaintext")
    assert r.returncode == 4

    clean = tmp_path / "clean"
    (clean / "profiles" / "solo").mkdir(parents=True)
    (clean / "config.yaml").write_text("agent:\n  reasoning_effort: high\n", encoding="utf-8")
    (clean / "profiles" / "solo" / "config.yaml").write_text(
        "providers:\n  anthropic:\n    api_key: ${ANTHROPIC_API_KEY}\n", encoding="utf-8")
    r2 = _run("--hermes-home", str(clean), "--fail-on-plaintext")
    assert r2.returncode == 0, r2.stdout + r2.stderr


def test_cli_missing_home():
    r = _run("--hermes-home", "/nonexistent/hermes/home/xyz")
    assert r.returncode == 1
