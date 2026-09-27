/* 折叠行（★ 定稿 G-3 的语法推广到计划 / 终端 / 文件 / 产物）。
 *
 * 五类旧卡片在批次十二之前还是"标题 + 等宽正文"的盒子，和工具行、推理行不是一套。
 * 批次十三统一：默认就是一行 `▸ <名字> · <预览>`，点开才有细节。
 *
 * 两条硬规则跟工具行一样：
 *  ① **折叠态里不许出现原始 JSON**——预览串过 `cleanLine()`，带 `{}[]` 的一律作废；
 *  ② **不常显时间戳**——时间挂在整行的 `title` 上，悬停才看得到（G-2 的同一条口径）。
 *
 * 视觉完全复用工具行那套 class（`.kaus-tool*`），不新造样式、不新造颜色（AD-78）。
 */

import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

/** 预览串上限：一行放得下。 */
const PREVIEW_MAX = 72;

/** 一眼能看出是结构体序列化的字符。 */
const JSONISH = /[{}[\]]/;

/**
 * 折叠态能显示的一行文字。
 *
 * 空串、带花括号 / 方括号的串一律返回 null——折叠态宁可只有名字，也不把
 * `{"cmd":…}` 摆到明面（G-3 最后一句，有断言测试盯着）。
 */
export function cleanLine(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const line = value.replace(/\s+/g, " ").trim();
  if (line === "" || JSONISH.test(line)) return null;
  return line.length > PREVIEW_MAX ? `${line.slice(0, PREVIEW_MAX)}…` : line;
}

export function RowCard({
  name,
  preview = null,
  tail = null,
  time,
  testId = "row-card",
  defaultOpen = false,
  children,
}: {
  /** 行首的名字：`计划` / `终端` / `修改了 a.txt` / `产物`。 */
  name: string;
  /** `·` 之后的预览串。传进来之前请先过 `cleanLine()`。 */
  preview?: string | null;
  /** 行尾的小字（计划的 `2/5 完成` 之类）。 */
  tail?: ReactNode;
  /** 悬停才显示的时间——**不**渲染成常显的 `00:00`。 */
  time?: string;
  testId?: string;
  defaultOpen?: boolean;
  /** 展开后的内容。没有内容时整行不可折叠（也就没有箭头）。 */
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const Chevron = open ? ChevronDown : ChevronRight;
  const expandable = Boolean(children);

  return (
    <div className="kaus-tool" data-testid={testId} title={time}>
      <button
        type="button"
        className="kaus-tool-head"
        aria-expanded={expandable ? open : undefined}
        disabled={!expandable}
        onClick={() => setOpen((value) => !value)}
      >
        {expandable ? (
          <Chevron className="size-3 kaus-tool-chevron" />
        ) : (
          <span className="size-3 kaus-tool-chevron" aria-hidden />
        )}
        <span className="kaus-tool-name">{name}</span>
        {preview !== null && <span className="kaus-tool-preview">{preview}</span>}
        {tail !== null && <span className="kaus-tool-tail">{tail}</span>}
      </button>
      {open && expandable && <div className="kaus-tool-body">{children}</div>}
    </div>
  );
}

/** 连续同类条目合成一组（G-3 的工具组，批次十三推广到文件组）：
 *  组头一行「运行了 N 个工具」/「修改了 N 个文件」，默认折叠，展开后逐行。 */
export function RowGroup({
  name,
  testId,
  children,
}: {
  name: string;
  testId: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="kaus-tool-group" data-testid={testId}>
      <button
        type="button"
        className="kaus-tool-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Chevron className="size-3 kaus-tool-chevron" />
        <span className="kaus-tool-name">{name}</span>
      </button>
      {open && <div className="kaus-tool-group-body">{children}</div>}
    </div>
  );
}
