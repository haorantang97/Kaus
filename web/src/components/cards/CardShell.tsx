/* 七种卡片共用的骨架（DESIGN.md §6.1，用现有 token 实现）。
 *
 * 头部固定四段：引擎标签（中性 chip）· 卡片类型 · 时间（右对齐）· 折叠箭头（可选）。
 * 状态**只**通过左侧一条 3px 竖条表达，正文文字永远是墨色——这是全局唯一的"状态上色"位置。
 * 流式中：时间位置换成状态点 + "生成中"，正文末尾一个不闪烁的块状光标（见 StreamCursor）。
 *
 * 这里不认识任何引擎：`engine` 是调用方给的纯展示文本（字母标签 + 显示名）。
 */

import { useState, type CSSProperties, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { useLocale } from "../../i18n";
import { cream } from "../ui";

export type CardStatus = "none" | "error" | "warn" | "info";

/** 状态色：沿用现有变量，不新增调色板。`--warning` 全仓一直吃回退值，这里照旧。 */
const STATUS_COLOR: Record<Exclude<CardStatus, "none">, string> = {
  error: "var(--danger)",
  warn: "var(--warning, #d98a55)",
  info: "var(--accent-text)",
};

export interface EngineLabel {
  /** 短标签，如 `H` / `C` / `CC`（DESIGN §2.4：引擎靠标签与图标区分，不分配颜色）。 */
  label: string;
  /** 引擎显示名。 */
  name: string;
}

export function CardShell({
  engine,
  type,
  time,
  status = "none",
  streaming = false,
  strongBorder = false,
  collapsible = false,
  defaultCollapsed = false,
  right,
  children,
}: {
  engine?: EngineLabel;
  type: string;
  time?: string;
  status?: CardStatus;
  streaming?: boolean;
  /** 审批卡：边框加重，但**不换底色**（DESIGN §6.2）。 */
  strongBorder?: boolean;
  collapsible?: boolean;
  defaultCollapsed?: boolean;
  right?: ReactNode;
  children: ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  /* H-1：加重边框是会话工作区里**仅有的两处**强调色之一（另一处是运行状态点）。
     底色仍然不换（DESIGN §6.2），只有那一圈 1.5px 的边。 */
  const frame: CSSProperties = strongBorder
    ? { background: cream(2), border: "1.5px solid var(--gold)", borderRadius: 8 }
    : {
        background: cream(2),
        border: `1px solid ${cream(16)}`,
        borderRadius: 8,
        borderLeft: status === "none" ? undefined : `3px solid ${STATUS_COLOR[status]}`,
      };

  return (
    <section
      className={`kaus-card overflow-hidden ${strongBorder ? "kaus-accent" : ""}`}
      style={frame}
      data-card-status={status}
      data-accent={strongBorder ? "approval" : undefined}
    >
      <header
        className="flex items-center gap-2 px-3"
        style={{ minHeight: 24, paddingTop: 6, paddingBottom: 6 }}
      >
        {engine && (
          <span
            className="inline-flex items-center rounded-[1px] px-1.5 py-0.5 text-[0.6rem] uppercase tracking-[0.14em]"
            style={{ background: cream(7), color: cream(70), border: `1px solid ${cream(14)}` }}
            title={engine.name}
          >
            {engine.label}
          </span>
        )}
        {engine && (
          <span className="text-xs" style={{ color: cream(70) }}>
            {engine.name}
          </span>
        )}
        <span className="text-xs" style={{ color: cream(62) }}>
          {type}
        </span>
        <span className="ml-auto flex items-center gap-2">
          {right}
          {/* 批次十三：时间不再常显——悬停整张卡才淡出（与消息的 `.kaus-msg-time` 同一条口径）。 */}
          {streaming ? <RunningTick /> : time && (
            <time className="kaus-card-time tabular-nums">{time}</time>
          )}
          {collapsible && (
            <button
              type="button"
              onClick={() => setCollapsed((value) => !value)}
              aria-expanded={!collapsed}
              className="grid size-5 place-items-center rounded hover:bg-black/[0.05]"
              style={{ color: cream(45) }}
            >
              <Chevron className="size-3.5" />
            </button>
          )}
        </span>
      </header>
      {!collapsed && <div className="px-3 pb-3">{children}</div>}
    </section>
  );
}

/** 运行中：中性色圆点 + 脉冲 + "生成中"（DESIGN §6.4：运行中不上彩色）。 */
export function RunningTick() {
  const { t } = useLocale();
  return (
    <span className="flex items-center gap-1.5 text-[0.65rem]" style={{ color: cream(50) }}>
      <span
        className="inline-block size-1.5 animate-pulse rounded-full"
        style={{ background: cream(50) }}
      />
      {t("card.running")}
    </span>
  );
}

/** 流式正文末尾的块状光标：1ch 宽、中性色、**不闪烁**（长回答里闪烁光标很吵）。 */
export function StreamCursor() {
  return (
    <span
      aria-hidden
      className="ml-0.5 inline-block align-[-0.1em]"
      style={{ width: "1ch", height: "1em", background: cream(38) }}
    />
  );
}

/** 卡片正文里的等宽块（工具参数 / 输出 / 原始错误 / diff 共用）。 */
export function MonoBlock({ children, max = 220 }: { children: ReactNode; max?: number }) {
  return (
    <pre
      className="overflow-auto whitespace-pre-wrap break-words rounded px-2 py-1.5 text-[0.7rem] leading-relaxed"
      style={{
        maxHeight: max,
        background: cream(4),
        border: `1px solid ${cream(11)}`,
        color: cream(70),
        font: "0.7rem/1.55 var(--theme-font-mono, monospace)",
      }}
    >
      {children}
    </pre>
  );
}

/** 正文里的小标题（工具卡的"调用 / 输出"这类栏头）。 */
export function ColumnLabel({ children }: { children: ReactNode }) {
  return (
    <div
      className="mb-1 text-[0.6rem] uppercase tracking-[0.14em]"
      style={{ color: cream(45) }}
    >
      {children}
    </div>
  );
}

export function stringify(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
