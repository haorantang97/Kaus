#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# restore_baseline.sh — 把 backup_baseline.sh 的备份写回 HERMES_HOME
#
# 用法：
#   scripts/ops/restore_baseline.sh <备份目录>              # 默认 dry-run：只打印将覆盖的文件
#   scripts/ops/restore_baseline.sh <备份目录> --apply      # 真正写入（会先打印同一份计划）
#
# 设计：
#   * 默认 dry-run —— 不带 --apply 绝不写任何文件。
#   * 覆盖式叠加还原：只写回备份里有的文件，不删除多出来的文件（不是 mirror）。
#   * 写之前先按 MANIFEST.txt 校验备份自身完整性（--skip-verify 可跳过）。
#   * 写之前把「即将被覆盖的当前文件」另存成一个 pre-restore 快照，方便再回滚一次。
#   * symlink 目标一律不覆盖（分身共享 config/skills/memories 是 symlink，
#     用实体文件覆盖会破坏 twin 语义）—— 命中就报告并跳过。
#   * skills/ 布局与 memories 链接不自动还原（备份里只有清单），
#     只打印「现状 vs 备份记录」的差异，由人手工修。
#   * 结束时打印两条 launchctl 重启命令。
#
# 环境变量：
#   HERMES_HOME         默认 ~/.hermes（也可 --hermes-home 覆盖）
#   HERMES_BACKUP_ROOT  pre-restore 快照落点的父目录，默认 $HERMES_HOME/dashboard-backups
#
# 只用系统自带工具，无第三方依赖，不需要 python。
# ---------------------------------------------------------------------------
set -euo pipefail

PROG="$(basename "$0")"
SRC=""
APPLY=0
SKIP_VERIFY=0
NO_PRE_BACKUP=0
ASSUME_YES=0
QUIET=0
HERMES_HOME_ARG=""

usage() {
  cat <<'EOF'
restore_baseline.sh — 还原 backup_baseline.sh 的备份

用法:
  restore_baseline.sh <备份目录> [选项]

选项:
  --apply             真正写入（默认只 dry-run）
  --yes               --apply 时不再交互确认
  --hermes-home PATH  还原目标 Hermes home（默认 $HERMES_HOME，再默认 ~/.hermes）
  --skip-verify       跳过 MANIFEST.txt 的 sha256 校验（不推荐）
  --no-pre-backup     --apply 时不做 pre-restore 快照（不推荐）
  --quiet             少打印
  -h, --help          本帮助

退出码:
  0 成功  1 出错  2 参数错  3 备份校验失败
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
    --apply)          APPLY=1; shift ;;
    --yes|-y)         ASSUME_YES=1; shift ;;
    --hermes-home)    need_arg "$1" $#; HERMES_HOME_ARG="$2"; shift 2 ;;
    --skip-verify)    SKIP_VERIFY=1; shift ;;
    --no-pre-backup)  NO_PRE_BACKUP=1; shift ;;
    --quiet)          QUIET=1; shift ;;
    --dry-run)        APPLY=0; shift ;;      # 显式写出来也接受（默认就是它）
    -h|--help)        usage; exit 0 ;;
    -*) printf '%s: 未知参数 %s（--help 看用法）\n' "$PROG" "$1" >&2; exit 2 ;;
    *)  if [ -n "$SRC" ]; then printf '%s: 只能给一个备份目录\n' "$PROG" >&2; exit 2; fi
        SRC="$1"; shift ;;
  esac
done

log()  { if [ "$QUIET" -eq 0 ]; then printf '%s\n' "$*"; fi; }
warn() { printf '%s\n' "$*" >&2; }
die()  { printf '%s: %s\n' "$PROG" "$*" >&2; exit "${2:-1}"; }

if [ -z "$SRC" ]; then usage >&2; exit 2; fi
SRC="${SRC%/}"
if [ ! -d "$SRC" ]; then die "备份目录不存在: $SRC"; fi

MANIFEST="$SRC/MANIFEST.txt"
RESTORE_MAP="$SRC/RESTORE_MAP.tsv"
for f in "$MANIFEST" "$RESTORE_MAP"; do
  if [ ! -f "$f" ]; then die "不像是 backup_baseline.sh 的备份（缺 $(basename "$f")）: $SRC"; fi
done

HHOME="${HERMES_HOME_ARG:-${HERMES_HOME:-$HOME/.hermes}}"
HHOME="${HHOME%/}"
if [ ! -d "$HHOME" ]; then die "HERMES_HOME 不存在: $HHOME"; fi

# --- sha256 ----------------------------------------------------------------
if command -v shasum >/dev/null 2>&1; then SHA_TOOL=shasum
elif command -v sha256sum >/dev/null 2>&1; then SHA_TOOL=sha256sum
elif command -v openssl >/dev/null 2>&1; then SHA_TOOL=openssl
else SHA_TOOL=""
fi

sha256_of() {
  case "$SHA_TOOL" in
    shasum)    shasum -a 256 "$1" | awk '{print $1}' ;;
    sha256sum) sha256sum "$1"     | awk '{print $1}' ;;
    openssl)   openssl dgst -sha256 "$1" | awk '{print $NF}' ;;
    *)         printf '' ;;
  esac
}

# ===========================================================================
# 0. 校验备份自身
# ===========================================================================
if [ "$SKIP_VERIFY" -eq 1 ]; then
  warn "!! 已跳过 MANIFEST 校验（--skip-verify）"
elif [ -z "$SHA_TOOL" ]; then
  warn "!! 找不到 shasum / sha256sum / openssl，无法校验 MANIFEST，继续。"
else
  log "== 0. 校验备份完整性（MANIFEST.txt）"
  bad=0; checked=0
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    want="${line%% *}"
    rel="${line#*  }"
    if [ ! -f "$SRC/$rel" ]; then warn "  缺文件: $rel"; bad=$((bad+1)); continue; fi
    got="$(sha256_of "$SRC/$rel")"
    if [ "$got" != "$want" ]; then warn "  校验不符: $rel"; bad=$((bad+1)); fi
    checked=$((checked+1))
  done < "$MANIFEST"
  if [ "$bad" -ne 0 ]; then
    die "备份完整性校验失败：$bad 项异常（共检查 $checked 项）。修好或用 --skip-verify 强来。" 3
  fi
  log "   OK：$checked 个文件校验通过"
fi

# ===========================================================================
# 1. 生成还原计划
# ===========================================================================
log ""
log "== 1. 还原计划"
log "   备份目录 : $SRC"
log "   还原目标 : $HHOME"
log ""

PLAN_OVERWRITE=""   # 每行: <backup_rel>\t<abs_target>
PLAN_CREATE=""
PLAN_SKIP=""
N_OVER=0; N_NEW=0; N_SKIP=0; N_MISS=0

while IFS="$(printf '\t')" read -r brel trel; do
  case "$brel" in ''|'#'*) continue ;; esac
  if [ -z "${trel:-}" ]; then continue; fi
  if [ ! -f "$SRC/$brel" ]; then
    warn "  备份里缺: $brel（RESTORE_MAP 有记录）"
    N_MISS=$((N_MISS+1))
    continue
  fi
  abs="$HHOME/$trel"
  if [ -L "$abs" ]; then
    PLAN_SKIP="$PLAN_SKIP$trel	目标是符号链接，不覆盖
"
    N_SKIP=$((N_SKIP+1))
  elif [ -e "$abs" ]; then
    if [ -n "$SHA_TOOL" ] && [ "$(sha256_of "$abs")" = "$(sha256_of "$SRC/$brel")" ]; then
      PLAN_SKIP="$PLAN_SKIP$trel	内容已一致，无需写
"
      N_SKIP=$((N_SKIP+1))
    else
      PLAN_OVERWRITE="$PLAN_OVERWRITE$brel	$abs
"
      N_OVER=$((N_OVER+1))
    fi
  else
    PLAN_CREATE="$PLAN_CREATE$brel	$abs
"
    N_NEW=$((N_NEW+1))
  fi
done < "$RESTORE_MAP"

if [ "$N_OVER" -gt 0 ] && [ "$QUIET" -eq 0 ]; then
  log "   将被【覆盖】的文件（$N_OVER）:"
  printf '%s' "$PLAN_OVERWRITE" | while IFS="$(printf '\t')" read -r b a; do
    if [ -n "${a:-}" ]; then printf '     M %s\n' "$a"; fi
  done
fi
if [ "$N_NEW" -gt 0 ] && [ "$QUIET" -eq 0 ]; then
  log "   将被【新建】的文件（$N_NEW）:"
  printf '%s' "$PLAN_CREATE" | while IFS="$(printf '\t')" read -r b a; do
    if [ -n "${a:-}" ]; then printf '     + %s\n' "$a"; fi
  done
fi
if [ "$N_SKIP" -gt 0 ] && [ "$QUIET" -eq 0 ]; then
  log "   跳过（$N_SKIP）:"
  printf '%s' "$PLAN_SKIP" | while IFS="$(printf '\t')" read -r t r; do
    if [ -n "${t:-}" ]; then printf '     - %s  (%s)\n' "$HHOME/$t" "$r"; fi
  done
fi
log ""
log "   合计: 覆盖 $N_OVER / 新建 $N_NEW / 跳过 $N_SKIP / 备份缺失 $N_MISS"

# ===========================================================================
# 2. 布局差异（skills / memories）—— 只报告，不自动还原
# ===========================================================================
log ""
log "== 2. 布局差异（skills/ 与 memories，只报告不还原）"

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
  if [ -d "$p" ]; then ( cd "$p" 2>/dev/null && pwd -P ) || printf '%s\n' "$p"
  else ( cd "$(dirname "$p")" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "$(basename "$p")" ) \
         || printf '%s\n' "$p"
  fi
}
link_target() { readlink "$1" 2>/dev/null || printf '<unreadable>'; }
describe_entry() {
  local p="$1"
  if [ -L "$p" ]; then printf 'symlink -> %s (realpath: %s)' "$(link_target "$p")" "$(abs_real "$p")"
  elif [ -d "$p" ]; then printf 'dir'
  elif [ -f "$p" ]; then printf 'regular file'
  elif [ -e "$p" ]; then printf 'other'
  else printf 'missing'; fi
}

LAYOUT_DIFFS=0
for pd_slash in "$SRC"/profiles/*/; do
  if [ ! -d "$pd_slash" ]; then continue; fi
  pd="${pd_slash%/}"
  name="$(basename "$pd")"
  if [ "$name" = "default" ]; then tgt_dir="$HHOME"; else tgt_dir="$HHOME/profiles/$name"; fi

  if [ ! -d "$tgt_dir" ]; then
    log "   ! profile 目录不存在（备份里有，现状没有）: $tgt_dir"
    LAYOUT_DIFFS=$((LAYOUT_DIFFS+1))
    continue
  fi

  # memories
  if [ -f "$pd/memories.link.txt" ]; then
    was="$(sed -n 's/^state: //p' "$pd/memories.link.txt" | head -1)"
    now="$(describe_entry "$tgt_dir/memories")"
    if [ "$was" != "$now" ]; then
      log "   ! $name  memories 变了"
      log "       备份记录: ${was:-<无>}"
      log "       当前现状: $now"
      LAYOUT_DIFFS=$((LAYOUT_DIFFS+1))
    fi
  fi

  # skills symlink 布局（只比 readlink 段）
  if [ -f "$pd/skills.listing.txt" ]; then
    want_links="$(sed -n '/^## readlink/,$p' "$pd/skills.listing.txt" | tail -n +2 | sed '/^$/d' | sort)"
    now_links="$( { find "$tgt_dir/skills/" -mindepth 1 -maxdepth 1 -type l -print0 2>/dev/null \
        | while IFS= read -r -d '' e; do printf '%s -> %s\n' "$(basename "$e")" "$(link_target "$e")"; done; } | sed '/^$/d' | sort )"
    if [ -z "$now_links" ]; then now_links="(没有符号链接条目)"; fi
    if [ "$want_links" != "$now_links" ]; then
      log "   ! $name  skills/ 符号链接布局与备份记录不一致"
      log "       详见 $pd/skills.listing.txt（本脚本不自动重建 symlink）"
      LAYOUT_DIFFS=$((LAYOUT_DIFFS+1))
    fi
  fi
done
if [ "$LAYOUT_DIFFS" -eq 0 ]; then log "   无差异。"; fi

# ===========================================================================
# 3. dry-run 到此为止
# ===========================================================================
if [ "$APPLY" -eq 0 ]; then
  log ""
  log "== DRY RUN —— 没有写任何文件。"
  log "   确认无误后执行:"
  log "     $0 '$SRC' --apply"
  exit 0
fi

if [ "$ASSUME_YES" -eq 0 ]; then
  printf '\n确认把上面 %s 个覆盖 + %s 个新建写入 %s ? [y/N] ' "$N_OVER" "$N_NEW" "$HHOME"
  read -r ans || ans=""
  case "$ans" in y|Y|yes|YES) ;; *) log "已取消。"; exit 0 ;; esac
fi

# ===========================================================================
# 4. pre-restore 快照 + 写入
# ===========================================================================
BACKUP_ROOT="${HERMES_BACKUP_ROOT:-$HHOME/dashboard-backups}"
PRE=""
if [ "$NO_PRE_BACKUP" -eq 0 ]; then
  # 目录名必须唯一：从一个 pre-restore 快照回滚时，如果新快照撞上同一秒的名字，
  # 就会把正在读的 RESTORE_MAP.tsv / MANIFEST.txt 截断 —— 那等于把源备份毁掉。
  PRE_BASE="$BACKUP_ROOT/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
  PRE="$PRE_BASE"
  pre_n=1
  while [ -e "$PRE" ]; do
    PRE="$PRE_BASE-$pre_n"
    pre_n=$((pre_n + 1))
  done
  if [ "$PRE" = "$SRC" ]; then die "pre-restore 快照目录与备份源相同，拒绝执行: $PRE"; fi
  mkdir -p "$PRE"
  chmod 700 "$PRE" 2>/dev/null || true
  printf '# backup_rel_path\ttarget_path_relative_to_HERMES_HOME\n' > "$PRE/RESTORE_MAP.tsv"
  : > "$PRE/MANIFEST.txt"
  : > "$PRE/CREATED.txt"
  {
    printf 'kind: pre-restore snapshot\n'
    printf 'created_utc: %s\n' "$(date -u +%Y%m%dT%H%M%SZ)"
    printf 'hermes_home: %s\n' "$HHOME"
    printf 'restored_from: %s\n' "$SRC"
  } > "$PRE/BACKUP_INFO.txt"
  log ""
  log "== 4a. pre-restore 快照: $PRE"
fi

snapshot_current() {   # snapshot_current <backup_rel> <abs_target> <target_rel>
  if [ -z "$PRE" ]; then return 0; fi
  mkdir -p "$PRE/$(dirname "$1")"
  cp -p "$2" "$PRE/$1"
  if [ -n "$SHA_TOOL" ]; then printf '%s  %s\n' "$(sha256_of "$PRE/$1")" "$1" >> "$PRE/MANIFEST.txt"; fi
  printf '%s\t%s\n' "$1" "$3" >> "$PRE/RESTORE_MAP.tsv"
}

log ""
log "== 4b. 写入"
WROTE=0
while IFS="$(printf '\t')" read -r brel trel; do
  case "$brel" in ''|'#'*) continue ;; esac
  if [ -z "${trel:-}" ]; then continue; fi
  if [ ! -f "$SRC/$brel" ]; then continue; fi
  abs="$HHOME/$trel"
  if [ -L "$abs" ]; then continue; fi
  if [ -e "$abs" ]; then
    if [ -n "$SHA_TOOL" ] && [ "$(sha256_of "$abs")" = "$(sha256_of "$SRC/$brel")" ]; then continue; fi
    snapshot_current "$brel" "$abs" "$trel"
  else
    if [ -n "$PRE" ]; then printf '%s\n' "$abs" >> "$PRE/CREATED.txt"; fi
  fi
  mkdir -p "$(dirname "$abs")"
  # 原子写：先落 .tmp 再 mv —— 正在读 config.yaml 的 hermes 进程不会读到半截文件
  tmp="$abs.restore.tmp.$$"
  cp -p "$SRC/$brel" "$tmp"
  mv -f "$tmp" "$abs"
  log "   写入 $abs"
  WROTE=$((WROTE+1))
done < "$RESTORE_MAP"

if [ -n "$PRE" ] && [ "$WROTE" -eq 0 ]; then
  rm -rf "$PRE"          # 什么都没写，快照是空的，别留垃圾目录
  PRE=""
fi

log ""
log "还原完成：写入 $WROTE 个文件。"
if [ -n "$PRE" ]; then
  log "pre-restore 快照（可再回滚一次）: $PRE"
  log "  回滚: $0 '$PRE' --apply"
fi

# ===========================================================================
# 5. 重启常驻服务
# ===========================================================================
cat <<'EOF'

=====================================================================
还原后必须重启两个 launchd 常驻服务（后端没有 --reload，前端 Vite 需要重载）：

  launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard.backend
  launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard.web

自检（改树 / 继承 / 还原后都该跑）：

  cd ~/.hermes/dashboard && /opt/homebrew/bin/python3.11 selfcheck.py

前端静态产物 dist* 不在备份内；桌面端 / :8877 需要时重新构建：

  cd ~/.hermes/dashboard/web && npm run build
=====================================================================
EOF

if command -v id >/dev/null 2>&1; then
  printf '（本机 uid 已展开的版本）\n'
  printf '  launchctl kickstart -k gui/%s/com.hermes.dashboard.backend\n' "$(id -u)"
  printf '  launchctl kickstart -k gui/%s/com.hermes.dashboard.web\n' "$(id -u)"
fi
