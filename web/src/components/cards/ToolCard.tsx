/* 工具行（★ 定稿 G-3）：折叠态就是一行 `▸ 工具名 · 预览串 · 耗时/状态`。
 *
 * 硬规则：**折叠态里不许出现原始 JSON**。预览串由 `toolPreview()` 挑，挑不出干净的
 * 短串就只显示工具名——宁可少显示，也不把 `{"cmd":…}` 摆到明面（G-3 最后一句，
 * 有断言测试盯着）。
 *
 * AD-126（批次十五第 1 件）：输出栏不再受能力门控——**有 output 就显示**，
 * 没有就整块不渲染（不留空栏、不留占位、不写备注）。`showOutput` 只剩预览页
 * 那一处显式关掉输出栏的用途，缺省是 true。
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { useLocale } from "../../i18n";
import { ColumnLabel, stringify } from "./CardShell";
import { RowGroup } from "./RowCard";
import type { ToolItem } from "./timelineReducer";
import { ToolResultBlocks } from "./ToolResultBlocks";

/** 预览串的长度上限：一行放得下，超了截断（截断本身不引入省略号以外的装饰）。 */
const PREVIEW_MAX = 72;

/** 一眼能看出是 JSON / 对象序列化的字符：出现任何一个就整串作废。 */
const JSONISH = /[{}[\]]/;

/** 折叠态优先挑的字段（batch18 第 3 件）。回填来的入参里，真正一句话说得清这次调用
 *  干了什么的就是 `command`——`preview` 是引擎给的概括串，只在没有真入参时才轮到它。 */
const PREVIEW_KEYS = ["command", "preview"] as const;

/**
 * 折叠态的预览串。
 *
 * 顺序：`input.command` → `input.preview` → `input` 的首个字符串字段 →
 * `input` 本身是字符串 → 没有。任何一步拿到的串里只要带 `{` `}` `[` `]`，一律当成
 * "这是结构体不是给人看的"，返回 null（折叠态宁可只有工具名）。
 */
export function toolPreview(input: unknown): string | null {
  const clean = (value: unknown): string | null => {
    if (typeof value !== "string") return null;
    const line = value.replace(/\s+/g, " ").trim();
    if (line === "" || JSONISH.test(line)) return null;
    return line.length > PREVIEW_MAX ? `${line.slice(0, PREVIEW_MAX)}…` : line;
  };
  if (typeof input === "string") return clean(input);
  if (input === null || typeof input !== "object" || Array.isArray(input)) return null;
  const record = input as Record<string, unknown>;
  for (const key of PREVIEW_KEYS) {
    const hit = clean(record[key]);
    if (hit !== null) return hit;
  }
  for (const value of Object.values(record)) {
    const hit = clean(value);
    if (hit !== null) return hit;
  }
  return null;
}

/* ------------------------------------------------------------------ *
 * 输出的解包（batch18 第 3 件）
 * ------------------------------------------------------------------ *
 * 回填来的工具输出在真机上是 `{"output": "/Users/x\n'a'\nAGENTS.md"}` 这种形状：
 * 直接 pretty-print 会把换行转义成 `\n` 摆在一行里，等于把一段终端输出弄成了
 * 一串给机器看的 JSON。规矩：**只有一个字符串字段**的包装对象就把里面那段文本
 * 取出来原样显示（`MonoBlock` 是 `white-space: pre-wrap`，换行照旧看得见）；
 * 多字段的对象仍旧 pretty-print——那时候键名本身也是信息。
 */
const OUTPUT_KEYS = new Set(["output", "stdout", "text", "content", "result"]);

export function unwrapToolOutput(output: unknown): unknown {
  if (output === null || typeof output !== "object" || Array.isArray(output)) return output;
  const entries = Object.entries(output as Record<string, unknown>).filter(
    ([, value]) => value !== undefined && value !== null,
  );
  if (entries.length !== 1) return output;
  const [key, value] = entries[0];
  return typeof value === "string" && OUTPUT_KEYS.has(key) ? value : output;
}

/* ------------------------------------------------------------------ *
 * 展开态（batch17 第 2 件）
 * ------------------------------------------------------------------ *
 * 走查 verify5 ①：有的引擎的 SSE 只带 `preview`，展开后 CALL 区于是摆着
 * `{"preview":"pwd + 2 commands"}` —— 一段给机器看的 JSON 冒充"入参"。规矩改成：
 *
 *  ① 有真入参（回填来的 `progress.input`，或事件本来就带了字段）→ 按键值对渲染，
 *     等宽，对象**展开一层**；
 *  ② 只有 `preview` → 一行 `命令：pwd + 2 commands`，**不显示原始 JSON**；
 *  ③ 输出区有 `output` 才渲染（字符串直接显示，对象 pretty-print），没有就整块缺席
 *     （AD-71 的同一条精神）。
 */

/** 回填标记（AD-133）：`tool.updated{progress:{source:"native_history", input, …}}`。 */
interface ToolBackfill {
  input?: unknown;
  name?: string;
  nativeCallId?: string;
}

export function toolBackfill(progress: unknown): ToolBackfill | null {
  if (progress === null || typeof progress !== "object" || Array.isArray(progress)) return null;
  const record = progress as Record<string, unknown>;
  return record.source === "native_history" ? (record as ToolBackfill) : null;
}

/** 只有一个 `preview` 键（或干脆是个字符串）的入参 = 「说不出入参，只有一句预览」。 */
function previewOnly(input: unknown): boolean {
  if (typeof input === "string") return true;
  if (input === null || typeof input !== "object" || Array.isArray(input)) return true;
  const keys = Object.keys(input as Record<string, unknown>).filter(
    (key) => (input as Record<string, unknown>)[key] !== undefined,
  );
  return keys.length === 0 || (keys.length === 1 && keys[0] === "preview");
}

/** 一个值渲染成一行字：字符串原样，标量 String()，结构体退回紧凑序列化（只在第二层出现）。 */
function scalarText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/** 键值对一行；值是对象/数组就把它的条目再列一层（**只展开一层**）。 */
function KeyValue({ name, value }: { name: string; value: unknown }) {
  const nested =
    value !== null && typeof value === "object"
      ? Object.entries(value as Record<string, unknown>)
      : null;
  return (
    <div className="kaus-tool-kv" data-testid="tool-arg">
      <span className="kaus-tool-kv-key">{name}</span>
      {nested && nested.length > 0 ? (
        <span className="kaus-tool-kv-nested">
          {nested.map(([key, item]) => (
            <span key={key} className="kaus-tool-kv-sub">
              <span className="kaus-tool-kv-key">{key}</span>
              <span className="kaus-tool-kv-value">{scalarText(item)}</span>
            </span>
          ))}
        </span>
      ) : (
        <span className="kaus-tool-kv-value">{scalarText(value)}</span>
      )}
    </div>
  );
}

export function ToolCard({
  item,
  showOutput = true,
  seconds,
  defaultOpen = false,
  conversationId,
}: {
  item: ToolItem;
  /** 显式关掉输出栏（预览页用）。缺省 true：有 output 就显示（AD-126）。 */
  showOutput?: boolean;
  /** 页面按信封时间算出来的耗时（内核 item 不存时间戳）。拿不到就显示状态词。 */
  seconds?: number;
  defaultOpen?: boolean;
  conversationId?: string;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(defaultOpen);
  const Chevron = open ? ChevronDown : ChevronRight;
  const running = item.status === "running";
  const failed = item.status === "failed";
  /* 输出：先解包 `{output:"…"}` 这类只有一个字符串字段的壳，再交给 `MonoBlock`
     （pre-wrap）——终端输出的换行必须留住（batch18 第 3 件）。 */
  const output = stringify(unwrapToolOutput(item.output));
  const hasOutput = showOutput && output !== "";
  /* 入参的真源：回填来的优先（那才是真的入参），否则用事件自带的。 */
  const backfill = toolBackfill(item.progress);
  const input = backfill?.input !== undefined ? backfill.input : item.input;
  /* 折叠态预览也用这份真源：回填带来 `{command:"…"}` 之后，一行里就该是那条命令，
     而不是引擎最初那句概括串（batch18 第 3 件）。 */
  const preview = toolPreview(input);
  const hasInput = input !== null && input !== undefined;
  /* 只有 preview（有的引擎的 SSE 就是这样）→ 一行「命令：…」，不摆 JSON。 */
  const commandLine = hasInput && previewOnly(input) ? toolPreview(input) : null;
  const args = hasInput && !previewOnly(input) ? Object.entries(input as Record<string, unknown>) : [];
  const hasCall = args.length > 0 || commandLine !== null;
  /* 回填之外的 `progress` 才当进度显示；回填标记本身只换来行尾一个中性小点。 */
  const progressNote = backfill === null && item.progress !== null && item.progress !== undefined;
  const progressEntries =
    progressNote && typeof item.progress === "object" && !Array.isArray(item.progress)
      ? Object.entries(item.progress as Record<string, unknown>)
      : [];
  /* batch40（DESIGN ★L 第 5 条）：折起来又已经跑完的那一行，行尾那个「完成」是废字
     ——没有出错标记、没有耗时，本来就说明它跑完了。所以：耗时照显（那是信息），
     运行中 / 失败照显（那是状态），**只有"完成"这一档在折叠态里不出现**，展开后
     仍然有。 */
  const tail =
    seconds !== undefined
      ? t("card.tool.seconds", { seconds: seconds.toFixed(1) })
      : running
        ? t("card.tool.running")
        : failed
          ? t("card.tool.failed")
          : open
            ? t("card.tool.done")
            : null;

  return (
    <div className="kaus-tool" data-status={item.status} data-testid="tool-row">
      <button
        type="button"
        className="kaus-tool-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Chevron className="size-3 kaus-tool-chevron" />
        <span className="kaus-tool-name">{item.name || t("card.type.tool")}</span>
        {preview !== null && (
          <span className="kaus-tool-preview" data-testid="tool-preview">
            {preview}
          </span>
        )}
        <span className="kaus-tool-tail">
          {/* batch17：回填到达后行尾多一个中性小点，说明这条的入参/输出是补齐的。 */}
          {backfill !== null && (
            <span
              className="kaus-tool-backfill"
              data-testid="tool-backfilled"
              title={t("card.tool.backfilled")}
              aria-label={t("card.tool.backfilled")}
            />
          )}
          {/* 出错只把状态点染成 --danger，行文字保持墨色（G-3）。 */}
          {/* H-1：运行中的点是允许用强调色的那两处之一；失败的点走 --danger（不是强调色）。 */}
          {(running || failed) && (
            <span
              className={`kaus-tool-dot ${failed ? "is-failed" : "is-running kaus-accent"}`}
              data-accent={failed ? undefined : "run"}
            />
          )}
          {tail}
        </span>
      </button>
      {open && (hasCall || hasOutput || progressNote) && (
        <div className="kaus-tool-body">
          {hasCall && (
            <div>
              <ColumnLabel>{t("card.tool.call")}</ColumnLabel>
              {args.length > 0 ? (
                <div className="kaus-tool-args" data-testid="tool-args">
                  {args.map(([key, value]) => (
                    <KeyValue key={key} name={key} value={value} />
                  ))}
                </div>
              ) : (
                <div className="kaus-tool-command" data-testid="tool-command">
                  {t("card.tool.commandLine", { value: commandLine as string })}
                </div>
              )}
            </div>
          )}
          {hasOutput && (
            <div>
              <ColumnLabel>{t("card.tool.output")}</ColumnLabel>
              {/* G-3：输出最高 320px，超了在框内滚。 */}
              <ToolResultBlocks value={unwrapToolOutput(item.output)} conversationId={conversationId} />
            </div>
          )}
          {progressNote && (
            /* 进度也按同一条规矩来：结构体列成键值对，字符串原样一行——
               展开态里同样不摆 `{"pct":50}` 这种给机器看的串。 */
            <div className="kaus-tool-progress">
              {progressEntries.length > 0 ? (
                <div className="kaus-tool-args" data-testid="tool-progress">
                  {progressEntries.map(([key, value]) => (
                    <KeyValue key={key} name={key} value={value} />
                  ))}
                </div>
              ) : (
                t("card.tool.progress", { value: stringify(item.progress) })
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** 连续 ≥2 个工具行合成一组（G-3）：组头一行"运行了 N 个工具"，默认折叠。
 *  批次十三后组头的实现搬到了 `RowCard.tsx` 的 `RowGroup`（文件组共用同一套）。 */
export function ToolGroup({ count, children }: { count: number; children: React.ReactNode }) {
  const { t } = useLocale();
  return (
    <RowGroup name={t("card.tool.group", { count })} testId="tool-group">
      {children}
    </RowGroup>
  );
}
