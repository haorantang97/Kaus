# `scripts/ops` — Phase 0A 运维脚本（在用户 Mac 上跑）

Phase 0A 要求「**可一键还原** + **不读取或复制 Secret** + **继承键敏感值审计报告**」。
这个目录就是那三件事：

| 脚本 | 干什么 | 对应基线 |
|---|---|---|
| `backup_baseline.sh` | 备份 dashboard 树 + 每个 profile 里仪表盘会改的文件与 symlink 布局 | Phase 0A「完整备份」「可一键还原」 |
| `restore_baseline.sh` | 把备份写回去（默认 dry-run），并打印重启命令 | Phase 0A「可一键还原」 |
| `audit_inheritable_secrets.py` | 16 个继承键的敏感值审计，只出键路径与计数 | R-06 / §5.5 / §19 Q-17 |

**运行环境**：macOS，`/bin/bash`（3.2 也行）与 `/usr/bin/python3`（3.9+）即可，
**不需要任何第三方包**。有 `/opt/homebrew/bin/python3.11`（带 PyYAML）时审计更准，见 §3。

**假设的位置**：脚本随仪表盘仓库一起落在 `~/.hermes/dashboard/scripts/ops/`。
下面的命令都从 `~/.hermes/dashboard` 执行。

---

## 0. 一次性准备

```bash
cd ~/.hermes/dashboard
chmod +x scripts/ops/backup_baseline.sh scripts/ops/restore_baseline.sh
```

---

## 1. 备份（做任何 Phase 1 改造之前先跑这条）

```bash
cd ~/.hermes/dashboard
scripts/ops/backup_baseline.sh
```

输出的最后一行就是备份目录，形如
`~/.hermes/dashboard-backups/20260902T081500Z`。记下它。

想把备份放到别处（例如外置盘）：

```bash
scripts/ops/backup_baseline.sh --out /Volumes/Backup/hermes-baseline-20260902
# 或者：
HERMES_BACKUP_ROOT=/Volumes/Backup/hermes scripts/ops/backup_baseline.sh
```

其它参数：

```bash
scripts/ops/backup_baseline.sh --help
scripts/ops/backup_baseline.sh --hermes-home /path/to/other/.hermes   # 换 Hermes home
scripts/ops/backup_baseline.sh --include-git                          # 连 dashboard/.git 一起备
```

### 备份里有什么

```
<备份目录>/
├── BACKUP_INFO.txt        # 时间 / 主机 / uid / git HEAD / launchd 状态
├── MANIFEST.txt           # 每个文件的 sha256
├── RESTORE_MAP.tsv        # 备份内路径 -> 相对 HERMES_HOME 的还原目标
├── SKIPPED.txt            # 被排除的每一条路径 + 命中的规则（只有路径）
├── SYMLINKS.txt           # 记录到的符号链接及指向（不跟随、不复制）
├── RESTORE.md             # 人读的还原说明（这次备份的具体路径）
├── dashboard/             # dashboard 文件树
└── profiles/<name>/
    ├── config.yaml            ┐
    ├── config.yaml.dashbak    │ 只复制普通文件；symlink 一律跳过并记录
    ├── .dash_inherited.json   │
    ├── SOUL.md                ┘
    ├── profile.txt            # 各条目类型 + main_twin_candidate
    ├── skills.listing.txt     # ls -la + readlink，**没有技能内容**
    └── memories.link.txt      # memories 是否 symlink、指向哪
```

`profiles/default/` 就是 `~/.hermes` 本体（与 `server.py:_profile_dir` 同口径）。

### 备份里没有什么（安全红线）

`.env`、`credentials/`、`auth*`、任何名字含 `key` / `token` / `secret` / `password` /
`credential` 的文件，以及 `*.pem` / `*.p12` / `id_rsa*` 等私钥格式，**一律不读取、不复制**。
排除规则写在 `backup_baseline.sh` 顶部的 `SECRET_NAME_EXCLUDES` / `BULK_NAME_EXCLUDES`
两个数组里，改那两个数组就等于改安全策略。命中的路径（只有路径）进 `SKIPPED.txt`。

体积/派生物排除：`node_modules`、`tmp`、`archive`、`__pycache__`、`.pytest_cache`、`dist*`，
以及默认排除的 `.git`（HEAD 与 `git status --short` 已记进 `BACKUP_INFO.txt`；要历史就加
`--include-git`）。

> ⚠️ 规则宁可误伤：仓库里如果有叫 `useKeyboard.ts`、`tokens.css` 之类的源码文件，也会被跳过。
> 跑完瞄一眼 `SKIPPED.txt` 里 `secret-rule:` 那几行，确认没有你真正需要的文件。
>
> ⚠️ `config.yaml` 按 Phase 0A 要求被完整复制，而它**可能含明文凭据**（先跑 §3 的审计确认）。
> 备份目录权限已设成 `700`，但**不要**把它 rsync 到共享盘或提交进 git。

---

## 2. 校验备份

```bash
cd ~/.hermes/dashboard-backups/20260902T081500Z    # 换成你的备份目录
shasum -a 256 -c MANIFEST.txt | grep -v ': OK$' ; echo "exit=$?"
```

没有输出（`grep` 退出码 1）= 全部 OK。

---

## 3. 继承键敏感值审计（R-06 / Q-17）

先自检脚本本身（不碰真实 `~/.hermes`，用临时 fixture）：

```bash
cd ~/.hermes/dashboard
/usr/bin/python3 scripts/ops/audit_inheritable_secrets.py --self-test
```

然后对真实 Hermes home 跑（**只读**，只打开 `config.yaml` 与 `.dash_inherited.json`）：

```bash
# markdown 报告到屏幕
/usr/bin/python3 scripts/ops/audit_inheritable_secrets.py

# 同时落两个文件（放 dashboard-backups 里，别放进 git 仓库根）
/usr/bin/python3 scripts/ops/audit_inheritable_secrets.py \
    --md-out   ~/.hermes/dashboard-backups/audit-inheritable-secrets.md \
    --json-out ~/.hermes/dashboard-backups/audit-inheritable-secrets.json

# 只要 JSON
/usr/bin/python3 scripts/ops/audit_inheritable_secrets.py --json
```

**更准的一次**：`/usr/bin/python3` 没有 PyYAML，脚本会退回内置的最小 YAML 解析器；
仪表盘用的 `python3.11` 有 PyYAML，用它跑一遍结果最准（尤其是「仍纯继承 / 已被本地改写」那一列）：

```bash
/opt/homebrew/bin/python3.11 scripts/ops/audit_inheritable_secrets.py \
    --md-out ~/.hermes/dashboard-backups/audit-inheritable-secrets.md
```

两个解析器的结论应当一致；不一致就是内置解析器碰到了没覆盖的 YAML 写法，
用 `--parser builtin` 对拍一次能复现：

```bash
/opt/homebrew/bin/python3.11 scripts/ops/audit_inheritable_secrets.py --json > /tmp/a.json
/opt/homebrew/bin/python3.11 scripts/ops/audit_inheritable_secrets.py --json --parser builtin > /tmp/b.json
diff /tmp/a.json /tmp/b.json && echo "两个解析器结论一致"
```

其它参数：

```bash
--hermes-home PATH     # 换 Hermes home（默认 $HERMES_HOME，再默认 ~/.hermes）
--profile NAME         # 只审计某几个 profile（可重复）
--fail-on-plaintext    # 命中疑似明文凭据时退出码 4（给 CI 用）
```

### 报告怎么读

- **第 1 节**：16 个继承键 × （出现的 profile 数 / 含疑似明文 / 含 `${ENV}` 引用 /
  含可疑子键名 / 由仪表盘物化 / 台账里也有明文副本）。
- **第 2 节**：疑似明文凭据的**键路径**（例如 `providers.anthropic.api_key`）+ 计数 + profile 列表。
- **第 3 节**：只是 `${ENV}` / secret ref 的键路径 —— **引用而非明文**，单独归类。
- **第 5 节**：读 `.dash_inherited.json` 得出的物化台账，说明这些键是不是
  `_materialize_config` 沿组织树写进去的、还是被本地改写过。
- **第 7 节**：直接回答 §19 Q-17。

**报告里绝不出现任何配置值。** 脚本在打印前会做一次泄漏自检：拿**被判定为敏感的值**
（`plaintext_suspect` 的整值，以及 `reference` 值去掉 `${...}` / `$VAR` / `%VAR%` /
`{{VAR}}` 占位符后剩下的实体残余）跟输出对撞，一旦有值漏进输出就直接抛错退出而不是打印，
报错只说**分类与长度**、不说值。

守卫**不纳入**这些，避免误报：

- `benign` / `path_ref` / 空值 / 纯数字；
- 长度 < 12 的值 —— `default`、`medium`、`deepseek`、`high` 这类良性枚举与名字；
- 等于任一 profile 名或键路径分段的值 —— 它们本来就会作为 profile 名、键路径、
  章节文字合法地出现在报告里。

> 如果你真的碰到 `ValueLeak: 输出中出现了被判定为敏感的值（分类 …，长度 …）`，
> 那说明报告渲染确实把一个敏感值写进去了 —— 这是 bug，请把报错里的**分类与长度**
> （不要贴值）反馈回来，不要用改守卫的方式绕过。

### 判定口径（会误判的地方）

- 判定纯粹看**形状**：已知前缀（`sk-` / `sk-ant-` / `ghp_` / `xox*-` / `AKIA` / `AIza` / `glpat-` …）、
  叶子键名像凭据（`api_key` / `token` / `secret` / `password` …）、或长随机高熵串。
- `${VAR}` / `$VAR` / `env:NAME` / `secret_ref:...` 归为**引用**，不算明文。
- 纯数字（`max_tokens: 200000`）、路径（`~/.config/...`）、URL、模型 id（`claude-opus-4-...`）
  不会被误报。
- 只看**叶子键名**：`credential_pool_strategies.rotation: round_robin` 不会因为祖先键名叫
  `credential_...` 就被判成凭据。
- 自定义格式的凭据可能被漏判 → 人工复核以键路径为准。

---

## 4. 还原

**第一步永远是 dry-run**（不带 `--apply` 就不会写任何文件）：

```bash
cd ~/.hermes/dashboard
scripts/ops/restore_baseline.sh ~/.hermes/dashboard-backups/20260902T081500Z
```

它会打印：备份自身的 sha256 校验结果、将被**覆盖**/**新建**/**跳过**的每个文件、
以及 `skills/` 与 `memories` 的「现状 vs 备份记录」差异。

确认无误后：

```bash
scripts/ops/restore_baseline.sh ~/.hermes/dashboard-backups/20260902T081500Z --apply
```

会先要一次 `y/N` 确认（脚本化场景加 `--yes` 跳过），然后：

1. 把**即将被覆盖的当前文件**另存成 `~/.hermes/dashboard-backups/pre-restore-<UTC>/`
   （它本身也是一个合法的备份目录，可以再 `restore_baseline.sh <它> --apply` 滚回来）；
2. 原子写入（先 `.tmp` 再 `mv`，正在读 `config.yaml` 的 hermes 进程不会读到半截）；
3. 打印重启命令。

### 还原后必须做的两件事

```bash
launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard.backend
launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard.web
```

然后跑自检：

```bash
cd ~/.hermes/dashboard && /opt/homebrew/bin/python3.11 selfcheck.py    # 退出码 0 = 全绿
```

前端静态产物 `dist*` 不在备份里；桌面端 / `:8877` 需要时重新构建：

```bash
cd ~/.hermes/dashboard/web && npm run build
```

### 还原的边界（重要）

- **覆盖式叠加**，不是 mirror：只写回备份里有的文件，**不会删除**还原后多出来的文件。
  要「回到那一刻的精确状态」，得手工删掉多余文件。
- **symlink 目标一律不覆盖**：分身（twin）共享的 `config.yaml` / `skills` / `memories` 是
  symlink，用实体文件覆盖会破坏 twin 语义。命中就报告并跳过。
- **`skills/` 布局与 `memories` 链接不自动还原**：备份里只有清单（这是任务的硬要求），
  脚本只打印差异，重建 symlink 得手工做。
- **脚本自己也在被还原的树里**：`scripts/ops/` 在 `~/.hermes/dashboard` 下面，
  还原一个旧备份会把这几个脚本一起退回旧版。要还原到很旧的备份，先把
  `scripts/ops/` 复制到 `/tmp` 再从那里跑。

退出码：`0` 成功 / `1` 出错 / `2` 参数错 / `3` 备份完整性校验失败。

---

## 5. 保持 `_INHERITABLE_KEYS` 同步

`audit_inheritable_secrets.py` 顶部的 16 个继承键是从 `server.py` 的 `_INHERITABLE_KEYS`
**原样抄**的。改了 `server.py` 就得同步这里。校验一行：

```bash
cd ~/.hermes/dashboard
/opt/homebrew/bin/python3.11 - <<'PY'
import ast, re, pathlib, sys
sys.path.insert(0, "scripts/ops")
import audit_inheritable_secrets as A
src = pathlib.Path("server.py").read_text(encoding="utf-8")
keys = ast.literal_eval(re.search(r"^_INHERITABLE_KEYS\s*=\s*(\[.*?\])", src, re.S|re.M).group(1))
print("一致" if keys == A.INHERITABLE_KEYS else "不一致！\nserver.py: %s\nops:       %s" % (keys, A.INHERITABLE_KEYS))
PY
```

（`scripts/ops/tests/test_audit_inheritable_secrets.py::test_keys_match_server_py` 也在跑同一个断言。）

---

## 6. 跑测试（开发机 / 容器里）

```bash
python3 -m pytest scripts/ops/tests -q
```

测试全部在临时目录里造假 `HERMES_HOME`（含分身 symlink、密钥文件、体积目录、坏 YAML），
真跑两个 bash 脚本，断言安全红线生效、清单可校验、还原正确且不破坏 symlink，
以及审计报告里不出现任何值。**测试不会碰任何真实 `~/.hermes`。**

只做语法检查：

```bash
bash -n scripts/ops/backup_baseline.sh
bash -n scripts/ops/restore_baseline.sh
```

---

## 7. 一次完整的 Phase 0A 流程

```bash
cd ~/.hermes/dashboard

# ① 备份
B=$(scripts/ops/backup_baseline.sh | tail -1); echo "$B"

# ② 校验
( cd "$B" && shasum -a 256 -c MANIFEST.txt | grep -v ': OK$' ) ; echo "校验完毕"

# ③ 审计（两个 python 各跑一遍，结论应当一致）
/opt/homebrew/bin/python3.11 scripts/ops/audit_inheritable_secrets.py \
    --md-out "$B/audit-inheritable-secrets.md" \
    --json-out "$B/audit-inheritable-secrets.json"
/usr/bin/python3 scripts/ops/audit_inheritable_secrets.py --json > /tmp/audit-systempy.json

# ④ 演练一次还原（dry-run，不写任何东西）
scripts/ops/restore_baseline.sh "$B"
```

真正需要回滚时：

```bash
scripts/ops/restore_baseline.sh "$B" --apply
launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard.backend
launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard.web
cd ~/.hermes/dashboard && /opt/homebrew/bin/python3.11 selfcheck.py
```
