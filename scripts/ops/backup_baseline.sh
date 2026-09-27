#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# backup_baseline.sh — Phase 0A 迁移安全基线备份
#
# 备份「仪表盘会改动的东西」，使 Phase 1+ 的改造可一键回滚：
#   1) $HERMES_HOME/dashboard 的源码 / JSON 状态文件（文件树拷贝）
#   2) 每个 profile（default = $HERMES_HOME 本体；其余 = $HERMES_HOME/profiles/*）中
#      仪表盘会写的文件：config.yaml、config.yaml.dashbak、.dash_inherited.json、SOUL.md
#   3) 每个 profile 的 skills/ **符号链接布局**（只记录 ls -la 清单 + readlink 结果，
#      绝不复制技能内容）与 memories 是否为 symlink 及其指向
#
# 安全红线：绝不复制 .env / credentials / auth* / 任何名字里含 key|token|secret|password
#           的文件。排除规则见下方 SECRET_NAME_EXCLUDES（可审计清单），
#           被排除的每一条路径都会写进备份里的 SKIPPED.txt（只有路径，没有内容）。
#
# 用法：
#   scripts/ops/backup_baseline.sh [--hermes-home PATH] [--out DIR]
#                                  [--include-git] [--quiet]
#
# 环境变量：
#   HERMES_HOME         默认 ~/.hermes
#   HERMES_BACKUP_ROOT  默认 $HERMES_HOME/dashboard-backups
#
# 目标平台：macOS（bash 3.2 / BSD find / shasum）与 Linux（bash 5 / GNU find）都能跑。
# 只用系统自带工具，无第三方依赖，不需要 python。
# ---------------------------------------------------------------------------
set -euo pipefail

# ===========================================================================
# 排除规则（可审计清单 —— 改这里就等于改安全策略）
# ===========================================================================

# A. 安全红线：任何深度、任何位置命中即跳过（大小写不敏感 -iname）。
#    命中的路径只被记录到 SKIPPED.txt，内容永不读取、永不复制。
#    宁可误伤（例如源码里叫 useKeyboard.ts 的文件也会被跳过）——误伤项在
#    SKIPPED.txt 里可见，可以手工补。
SECRET_NAME_EXCLUDES=(
  '.env' '.env.*' '*.env'
  'credentials' 'credentials.*'
  'auth*'
  '*key*' '*token*' '*secret*' '*password*' '*passwd*' '*credential*'
  '*.pem' '*.p12' '*.pfx' '*.pkcs12' '*.keychain*'
  'id_rsa*' 'id_ed25519*' 'id_ecdsa*' 'id_dsa*'
  '.netrc' '.htpasswd' '.npmrc' '.pypirc'
)

# B. 体积 / 派生物排除：备份没有价值、且能重新生成的目录。
#    dist* 被排除 → 恢复后前端需要重新 `npm run build`（见 RESTORE.md）。
BULK_NAME_EXCLUDES=(
  'node_modules' 'tmp' 'archive' '__pycache__' '.pytest_cache'
  'dist' 'dist-*' 'dist_*'
)

# B2. .git 默认排除（dashboard 是 `hermes update` 的 git clone，历史可从 remote 取回；
#     HEAD 与 `git status --short` 已记进 BACKUP_INFO.txt）。
#     需要连历史一起备份时加 --include-git。
GIT_NAME_EXCLUDES=( '.git' )

# C. 每个 profile 里会被复制的文件（白名单，只有这几个）。
PROFILE_FILES=( 'config.yaml' 'config.yaml.dashbak' '.dash_inherited.json' 'SOUL.md' )

# D. 每个 profile 里只记录布局、绝不复制内容的条目。
PROFILE_LAYOUT_ONLY=( 'skills' 'memories' )

# ===========================================================================

PROG="$(basename "$0")"
QUIET=0
OUT_DIR=""
HERMES_HOME_ARG=""
INCLUDE_GIT=0

usage() {
  cat <<'EOF'
backup_baseline.sh — Phase 0A 迁移安全基线备份

用法:
  backup_baseline.sh [--hermes-home PATH] [--out DIR] [--include-git] [--quiet]

选项:
  --hermes-home PATH  Hermes home（默认 $HERMES_HOME，再默认 ~/.hermes）
  --out DIR           备份落点（默认 $HERMES_BACKUP_ROOT/<UTC 时间戳>，
                      $HERMES_BACKUP_ROOT 再默认 <hermes-home>/dashboard-backups）
  --include-git       连 dashboard/.git 一起备份（默认排除）
  --quiet             少打印
  -h, --help          本帮助

产出:
  <out>/BACKUP_INFO.txt   环境快照（时间、主机、hermes home、launchd 状态、git rev）
  <out>/MANIFEST.txt      每个备份文件的 sha256（可用 shasum -a 256 -c MANIFEST.txt 校验）
  <out>/RESTORE_MAP.tsv   备份内路径 -> 相对 HERMES_HOME 的还原目标（restore 脚本读它）
  <out>/SKIPPED.txt       被排除的每一条路径 + 原因（安全红线 / 体积 / symlink）
  <out>/SYMLINKS.txt      记录到的符号链接及其指向（不跟随、不复制）
  <out>/RESTORE.md        人读的还原说明
  <out>/dashboard/...     dashboard 文件树
  <out>/profiles/<name>/  各 profile 的白名单文件 + 布局记录

脚本最后一行 stdout 是备份目录的绝对路径（方便脚本化）。
EOF
}

need_arg() {   # need_arg <flag> <remaining argc>
  if [ "$2" -lt 2 ]; then
    printf '%s: %s 需要一个参数\n' "$PROG" "$1" >&2
    exit 2
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --hermes-home) need_arg "$1" $#; HERMES_HOME_ARG="$2"; shift 2 ;;
    --out)         need_arg "$1" $#; OUT_DIR="$2"; shift 2 ;;
    --include-git) INCLUDE_GIT=1; shift ;;
    --quiet)       QUIET=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) printf '%s: 未知参数 %s（--help 看用法）\n' "$PROG" "$1" >&2; exit 2 ;;
  esac
done

log() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*" >&2; }
die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit 1; }

if [ "$INCLUDE_GIT" -eq 0 ]; then
  BULK_NAME_EXCLUDES=( "${BULK_NAME_EXCLUDES[@]}" "${GIT_NAME_EXCLUDES[@]}" )
fi

# --- Hermes home ------------------------------------------------------------
HHOME="${HERMES_HOME_ARG:-${HERMES_HOME:-$HOME/.hermes}}"
HHOME="${HHOME%/}"
[ -d "$HHOME" ] || die "HERMES_HOME 不存在: $HHOME"

BACKUP_ROOT="${HERMES_BACKUP_ROOT:-$HHOME/dashboard-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${OUT_DIR:-$BACKUP_ROOT/$STAMP}"
DEST="${DEST%/}"

if [ -e "$DEST" ]; then die "备份目录已存在，换一个 --out: $DEST"; fi
case "$DEST/" in
  "$HHOME/dashboard/"*) die "备份目录不能放在 dashboard 树内部（会自我递归）: $DEST" ;;
esac

# --- sha256 工具 ------------------------------------------------------------
if command -v shasum >/dev/null 2>&1; then
  SHA_TOOL=shasum
elif command -v sha256sum >/dev/null 2>&1; then
  SHA_TOOL=sha256sum
elif command -v openssl >/dev/null 2>&1; then
  SHA_TOOL=openssl
else
  die "找不到 shasum / sha256sum / openssl，无法生成 MANIFEST"
fi

sha256_of() {
  case "$SHA_TOOL" in
    shasum)    shasum -a 256 "$1" | awk '{print $1}' ;;
    sha256sum) sha256sum "$1"     | awk '{print $1}' ;;
    openssl)   openssl dgst -sha256 "$1" | awk '{print $NF}' ;;
  esac
}

# 纯 shell 的 realpath（macOS 没有 GNU readlink -f）：先逐跳解开 symlink，再取物理路径。
abs_real() {
  local p="$1" i=0 t
  while [ -L "$p" ] && [ "$i" -lt 20 ]; do
    t="$(readlink "$p" 2>/dev/null)" || break
    case "$t" in
      /*) p="$t" ;;
      *)  p="$(dirname "$p")/$t" ;;
    esac
    i=$((i + 1))
  done
  if [ -d "$p" ]; then
    ( cd "$p" 2>/dev/null && pwd -P ) || printf '%s\n' "$p"
  else
    ( cd "$(dirname "$p")" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "$(basename "$p")" ) \
      || printf '%s\n' "$p"
  fi
}

link_target() { readlink "$1" 2>/dev/null || printf '<unreadable>'; }

# --- find 表达式 ------------------------------------------------------------
# FIND_PRUNE 是 "( -name A -o -iname B ... )"，接 -prune 用。
FIND_PRUNE=()
build_find_prune() {
  local first=1 p
  FIND_PRUNE=( '(' )
  for p in "${BULK_NAME_EXCLUDES[@]}"; do
    if [ "$first" -eq 0 ]; then FIND_PRUNE=( "${FIND_PRUNE[@]}" -o ); fi
    FIND_PRUNE=( "${FIND_PRUNE[@]}" -name "$p" )
    first=0
  done
  for p in "${SECRET_NAME_EXCLUDES[@]}"; do
    if [ "$first" -eq 0 ]; then FIND_PRUNE=( "${FIND_PRUNE[@]}" -o ); fi
    FIND_PRUNE=( "${FIND_PRUNE[@]}" -iname "$p" )
    first=0
  done
  FIND_PRUNE=( "${FIND_PRUNE[@]}" ')' )
}
build_find_prune

# 判定一条被 prune 的路径命中的是哪一类规则（只看 basename，不看内容）。
skip_reason() {
  local base lower p
  base="$(basename "$1")"
  lower="$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')"
  for p in "${SECRET_NAME_EXCLUDES[@]}"; do
    case "$lower" in
      $p) printf 'secret-rule:%s' "$p"; return ;;
    esac
  done
  for p in "${BULK_NAME_EXCLUDES[@]}"; do
    case "$base" in
      $p) printf 'bulk-rule:%s' "$p"; return ;;
    esac
  done
  printf 'other'
}

# --- 目录骨架 ---------------------------------------------------------------
mkdir -p "$DEST"
chmod 700 "$DEST" 2>/dev/null || true
mkdir -p "$DEST/dashboard" "$DEST/profiles"

MANIFEST="$DEST/MANIFEST.txt"
RESTORE_MAP="$DEST/RESTORE_MAP.tsv"
SKIPPED="$DEST/SKIPPED.txt"
SYMLINKS="$DEST/SYMLINKS.txt"

: > "$MANIFEST"
{
  printf '# 被排除的路径（只有路径，内容从未被读取）。\n'
  printf '# 说明：本清单覆盖 dashboard 树的扫描结果。profile 层是**白名单**备份\n'
  printf '#       （只碰 config.yaml / config.yaml.dashbak / .dash_inherited.json / SOUL.md），\n'
  printf '#       所以 profile 目录下的 .env / credentials/ / auth* 连枚举都没有发生。\n'
  printf '# reason\tpath\n'
} > "$SKIPPED"
printf '# link\t->\ttarget\n' > "$SYMLINKS"
printf '# backup_rel_path\ttarget_path_relative_to_HERMES_HOME\n' > "$RESTORE_MAP"

COPIED=0
SKIPPED_N=0
LINKS_N=0

record_map()      { printf '%s\t%s\n' "$1" "$2" >> "$RESTORE_MAP"; }
record_manifest() { printf '%s  %s\n' "$(sha256_of "$DEST/$1")" "$1" >> "$MANIFEST"; }
record_skip()     { printf '%s\t%s\n' "$2" "$1" >> "$SKIPPED"; SKIPPED_N=$((SKIPPED_N + 1)); }
record_link()     { printf '%s\t->\t%s\n' "$1" "$2" >> "$SYMLINKS"; LINKS_N=$((LINKS_N + 1)); }

# copy_file <src_abs> <backup_rel> <target_rel_to_hermes_home>
copy_file() {
  local src="$1" brel="$2" trel="$3"
  mkdir -p "$DEST/$(dirname "$brel")"
  cp -p "$src" "$DEST/$brel"
  chmod u+rw "$DEST/$brel" 2>/dev/null || true
  record_manifest "$brel"
  record_map "$brel" "$trel"
  COPIED=$((COPIED + 1))
}

# ===========================================================================
# 1. dashboard 树
# ===========================================================================
DASH_SRC="$HHOME/dashboard"
if [ -d "$DASH_SRC" ]; then
  log "[1/3] 备份 dashboard 树: $DASH_SRC"

  # 1a. 被排除的条目 -> SKIPPED.txt（只记路径）
  while IFS= read -r -d '' p; do
    record_skip "$p" "$(skip_reason "$p")"
  done < <(find "$DASH_SRC" "${FIND_PRUNE[@]}" -prune -print0 2>/dev/null)

  # 1b. 符号链接 -> 只记录指向，不跟随、不复制
  while IFS= read -r -d '' p; do
    record_link "dashboard/${p#"$DASH_SRC"/}" "$(link_target "$p")"
  done < <(find "$DASH_SRC" "${FIND_PRUNE[@]}" -prune -o -type l -print0 2>/dev/null)

  # 1c. 普通文件 -> 复制
  while IFS= read -r -d '' p; do
    rel="${p#"$DASH_SRC"/}"
    case "$rel" in
      *$'\t'*|*$'\n'*) record_skip "$p" "path-has-tab-or-newline"; continue ;;
    esac
    copy_file "$p" "dashboard/$rel" "dashboard/$rel"
  done < <(find "$DASH_SRC" "${FIND_PRUNE[@]}" -prune -o -type f -print0 2>/dev/null)
else
  log "[1/3] 跳过 dashboard 树（不存在: $DASH_SRC）"
fi

# ===========================================================================
# 2. 每个 profile
# ===========================================================================
log "[2/3] 备份 profile 配置与布局"

# profile 名列表：default（= $HHOME 本体）+ profiles/* 下的目录（glob 天然排序）
PROFILE_NAMES=( default )
for d in "$HHOME"/profiles/*/; do
  [ -d "$d" ] || continue
  PROFILE_NAMES=( "${PROFILE_NAMES[@]}" "$(basename "$d")" )
done

profile_dir() {
  if [ "$1" = "default" ]; then printf '%s' "$HHOME"; else printf '%s/profiles/%s' "$HHOME" "$1"; fi
}
profile_rel() {
  if [ "$1" = "default" ]; then printf '.'; else printf 'profiles/%s' "$1"; fi
}

describe_entry() {
  local p="$1"
  if [ -L "$p" ]; then
    printf 'symlink -> %s (realpath: %s)' "$(link_target "$p")" "$(abs_real "$p")"
  elif [ -d "$p" ]; then printf 'dir'
  elif [ -f "$p" ]; then printf 'regular file'
  elif [ -e "$p" ]; then printf 'other'
  else printf 'missing'
  fi
}

MAIN_MEM_REAL="$(abs_real "$HHOME/memories")"

for name in "${PROFILE_NAMES[@]}"; do
  pdir="$(profile_dir "$name")"
  prel="$(profile_rel "$name")"
  [ -d "$pdir" ] || continue
  mkdir -p "$DEST/profiles/$name"

  # 2a. profile.txt —— 结构记录（不含任何配置值）
  {
    printf 'profile: %s\n' "$name"
    printf 'dir: %s\n' "$pdir"
    printf 'dir_rel_to_hermes_home: %s\n' "$prel"
    for f in "${PROFILE_FILES[@]}"; do
      printf '%s: %s\n' "$f" "$(describe_entry "$pdir/$f")"
    done
    for f in "${PROFILE_LAYOUT_ONLY[@]}"; do
      printf '%s: %s\n' "$f" "$(describe_entry "$pdir/$f")"
    done
    # 分身判定（与 server.py is_main_twin 同口径：memories 是解析到 default/memories 的 symlink）
    twin=no
    if [ "$name" != "default" ] && [ -L "$pdir/memories" ]; then
      pm="$(abs_real "$pdir/memories")"
      if [ -n "$pm" ] && [ "$pm" = "$MAIN_MEM_REAL" ]; then twin=yes; fi
    fi
    printf 'main_twin_candidate: %s\n' "$twin"
  } > "$DEST/profiles/$name/profile.txt"
  record_manifest "profiles/$name/profile.txt"

  # 2b. 白名单文件 —— symlink 一律不复制
  #     （复制会把分身共享的 symlink 在还原时变成实体文件，破坏 twin 语义）
  for f in "${PROFILE_FILES[@]}"; do
    src="$pdir/$f"
    if [ -L "$src" ]; then
      record_skip "$src" "symlink-not-copied"
      record_link "$prel/$f" "$(link_target "$src")"
      continue
    fi
    [ -f "$src" ] || continue
    if [ "$prel" = "." ]; then trel="$f"; else trel="$prel/$f"; fi
    copy_file "$src" "profiles/$name/$f" "$trel"
  done

  # 2c. skills/ 布局 —— 只记录 ls -la + readlink，不复制任何技能内容
  {
    printf '# profile: %s\n' "$name"
    printf '# skills dir: %s\n' "$pdir/skills"
    printf '# 只记录布局；技能内容不在本备份内。\n\n'
    if [ -d "$pdir/skills" ] || [ -L "$pdir/skills" ]; then
      printf '## entry: %s\n\n' "$(describe_entry "$pdir/skills")"
      printf '## ls -la\n'
      ls -la "$pdir/skills/" 2>/dev/null || printf '(ls 失败)\n'
      printf '\n## readlink（仅符号链接条目）\n'
      found=0
      while IFS= read -r -d '' e; do
        printf '%s -> %s\n' "$(basename "$e")" "$(link_target "$e")"
        found=1
      done < <(find "$pdir/skills/" -mindepth 1 -maxdepth 1 -type l -print0 2>/dev/null)
      if [ "$found" -eq 0 ]; then printf '(没有符号链接条目)\n'; fi
    else
      printf '(no skills dir)\n'
    fi
  } > "$DEST/profiles/$name/skills.listing.txt"
  record_manifest "profiles/$name/skills.listing.txt"

  # 2d. memories 是否 symlink 及指向
  {
    printf 'path: %s\n' "$pdir/memories"
    printf 'state: %s\n' "$(describe_entry "$pdir/memories")"
  } > "$DEST/profiles/$name/memories.link.txt"
  record_manifest "profiles/$name/memories.link.txt"
done

# ===========================================================================
# 3. 环境快照 + RESTORE.md
# ===========================================================================
log "[3/3] 写 BACKUP_INFO.txt / RESTORE.md"

{
  printf 'created_utc: %s\n' "$STAMP"
  printf 'hostname: %s\n' "$(hostname 2>/dev/null || echo unknown)"
  printf 'uname: %s\n' "$(uname -a 2>/dev/null || echo unknown)"
  printf 'user: %s\n' "$(id -un 2>/dev/null || echo unknown)"
  printf 'uid: %s\n' "$(id -u 2>/dev/null || echo unknown)"
  printf 'hermes_home: %s\n' "$HHOME"
  printf 'backup_dir: %s\n' "$DEST"
  printf 'sha_tool: %s\n' "$SHA_TOOL"
  printf 'bash_version: %s\n' "${BASH_VERSION:-unknown}"
  printf 'include_git: %s\n' "$INCLUDE_GIT"
  printf 'profiles_backed_up: %s\n' "${#PROFILE_NAMES[@]}"
  printf 'files_copied: %s\n' "$COPIED"
  printf '\n## dashboard git rev\n'
  if [ -d "$DASH_SRC/.git" ] && command -v git >/dev/null 2>&1; then
    git -C "$DASH_SRC" rev-parse HEAD 2>/dev/null || printf '(git rev-parse 失败)\n'
    git -C "$DASH_SRC" status --short 2>/dev/null | head -50 || true
  else
    printf '(dashboard 不是 git 仓库，或本机没有 git)\n'
  fi
  printf '\n## launchd（仅任务名与状态，无凭据）\n'
  if command -v launchctl >/dev/null 2>&1; then
    launchctl list 2>/dev/null | grep -i hermes || printf '(没有匹配 hermes 的 launchd 任务)\n'
  else
    printf '(本机没有 launchctl —— 非 macOS)\n'
  fi
} > "$DEST/BACKUP_INFO.txt"
record_manifest "BACKUP_INFO.txt"
record_manifest "RESTORE_MAP.tsv"
record_manifest "SKIPPED.txt"
record_manifest "SYMLINKS.txt"

cat > "$DEST/RESTORE.md" <<EOF
# 还原说明（备份 $STAMP）

- 备份目录：\`$DEST\`
- 来源 HERMES_HOME：\`$HHOME\`
- 复制的文件数：$COPIED
- 被排除的路径数：$SKIPPED_N（见 \`SKIPPED.txt\`，只有路径）
- 记录的符号链接数：$LINKS_N（见 \`SYMLINKS.txt\`）

## 这份备份里有什么

| 内容 | 位置 | 说明 |
|---|---|---|
| dashboard 文件树 | \`dashboard/\` | 排除 node_modules / tmp / archive / \_\_pycache\_\_ / .pytest_cache / dist\*（默认还排除 .git） |
| 各 profile 的 config.yaml、config.yaml.dashbak、.dash_inherited.json、SOUL.md | \`profiles/<name>/\` | 只复制普通文件；symlink 一律不复制、只记录 |
| skills/ 符号链接布局 | \`profiles/<name>/skills.listing.txt\` | 只有 \`ls -la\` 清单与 readlink 结果，**没有技能内容** |
| memories 状态 | \`profiles/<name>/memories.link.txt\` | 是否 symlink、指向哪里 |
| 结构记录 | \`profiles/<name>/profile.txt\` | 各条目类型 + 是否分身（main_twin_candidate） |
| 校验 | \`MANIFEST.txt\` | \`shasum -a 256 -c MANIFEST.txt\`（在本目录内执行） |
| 还原映射 | \`RESTORE_MAP.tsv\` | 备份内路径 → 相对 HERMES_HOME 的目标路径 |

## 这份备份里**没有**什么（安全红线）

\`.env\`、\`credentials/\`、\`auth*\`、任何名字含 key / token / secret / password / credential 的
文件，以及私钥格式（\*.pem / \*.p12 / id_rsa\* 等）**一律没有被读取或复制**。它们的路径（仅路径）
列在 \`SKIPPED.txt\`。安全规则宁可误伤，如果里面有你确实需要的源码文件，请手工处理。

> ⚠️ \`config.yaml\` 按 Phase 0A 要求被完整复制，而 Hermes 的 \`providers\` /
> \`credential_pool_strategies\` 配置块**可能含明文凭据**。因此本备份目录权限是 700。
> 先跑 \`scripts/ops/audit_inheritable_secrets.py\` 看清楚哪些键携带凭据，再决定这个目录放哪、
> 要不要加密归档。

## 怎么还原

\`\`\`bash
# 1) 先看会覆盖什么（默认就是 dry-run，不写任何文件）
scripts/ops/restore_baseline.sh "$DEST"

# 2) 确认无误后真正执行
scripts/ops/restore_baseline.sh "$DEST" --apply
\`\`\`

还原是**覆盖式叠加**：只写回备份里有的文件，不会删除还原后多出来的文件。
skills/ 布局与 memories 链接**不会被自动还原**（备份里只有清单），restore 脚本会打印
"现状 vs 记录" 的差异，由你手工修。

## 还原后必须重启常驻服务

\`\`\`bash
launchctl kickstart -k gui/\$(id -u)/com.hermes.dashboard.backend
launchctl kickstart -k gui/\$(id -u)/com.hermes.dashboard.web
\`\`\`

前端 \`dist*\` 不在备份内；若桌面端 / :8877 需要静态产物，还原后再跑一次
\`cd $HHOME/dashboard/web && npm run build\`。
EOF
record_manifest "RESTORE.md"

chmod -R go-rwx "$DEST" 2>/dev/null || true

log ""
log "备份完成: $DEST"
log "  文件: $COPIED   排除: $SKIPPED_N   符号链接记录: $LINKS_N   profile: ${#PROFILE_NAMES[@]}"
log "  校验: (cd '$DEST' && shasum -a 256 -c MANIFEST.txt)"
log "  还原: scripts/ops/restore_baseline.sh '$DEST'            # dry-run"
log "        scripts/ops/restore_baseline.sh '$DEST' --apply    # 真正写入"
printf '%s\n' "$DEST"
