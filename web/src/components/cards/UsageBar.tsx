/* 用量条（DESIGN §6.2）：输入区上方一行小字，等宽数字，不是卡片。
 *
 * N §7.3 规则 6：不同引擎报的精度不同，公共层不伪造缺失字段——所以这里
 * **只渲染非空的项**，一项都没有时整行不渲染（AD-71：不留空壳、不写备注）。
 */

import { t } from "../../i18n";
import { cream, Pill } from "../ui";
import type { UsageSnapshot } from "./timelineReducer";

function formatCount(value: number): string {
  return value >= 10000 ? `${(value / 1000).toFixed(1)}k` : String(value);
}

/* 用量 Pill（★ 定稿 G-8）：从消息流上方挪到页头右侧，折成一枚 `总 30.7k`。
 * 悬停（`title` + 浮层）展开 in / out / total。一个数都没有时整枚不渲染（AD-71）。 */
export function UsagePill({ usage }: { usage: UsageSnapshot | null }) {
  const dash = t("conversation.usage.unknown");
  const cell = (value: number | null | undefined) => (value == null ? dash : formatCount(value));
  const total = usage?.totalTokens ?? null;
  if (
    usage == null ||
    (usage.totalTokens == null && usage.inputTokens == null && usage.outputTokens == null)
  ) {
    return null;
  }
  const detail = t("conversation.usage.title", {
    input: cell(usage.inputTokens),
    output: cell(usage.outputTokens),
    total: cell(total),
  });
  return (
    <span className="kaus-usage-pill" data-testid="usage-pill">
      <Pill tone="muted" title={detail}>
        {t("conversation.usage.pill", { value: cell(total ?? usage.outputTokens) })}
      </Pill>
      <span className="kaus-usage-pop" role="note">
        {detail}
      </span>
    </span>
  );
}

export function UsageBar({
  usage,
  model,
  durationMs,
}: {
  usage: UsageSnapshot | null;
  /** 会话当前模型（对话头部已有的信息，这里只是复述一次数字旁的上下文）。 */
  model?: string | null;
  durationMs?: number | null;
}) {
  const parts: string[] = [];
  if (usage?.inputTokens != null) parts.push(t("card.usage.input", { value: formatCount(usage.inputTokens) }));
  if (usage?.outputTokens != null) parts.push(t("card.usage.output", { value: formatCount(usage.outputTokens) }));
  if (usage?.reasoningTokens != null)
    parts.push(t("card.usage.reasoning", { value: formatCount(usage.reasoningTokens) }));
  if (usage?.totalTokens != null) parts.push(t("card.usage.total", { value: formatCount(usage.totalTokens) }));
  if (usage?.contextUsed != null && usage.contextWindow != null) {
    parts.push(
      t("card.usage.context", {
        used: formatCount(usage.contextUsed),
        window: formatCount(usage.contextWindow),
      }),
    );
  }
  if (usage?.costUsd != null) parts.push(`$${usage.costUsd.toFixed(4)}`);
  if (durationMs != null) parts.push(`${(durationMs / 1000).toFixed(1)}s`);
  if (model) parts.push(model);

  if (parts.length === 0) return null;
  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-1 px-1 py-1 text-[0.65rem] tabular-nums"
      style={{ color: cream(45), font: "0.65rem var(--theme-font-mono, monospace)" }}
    >
      {parts.map((part) => (
        <span key={part}>{part}</span>
      ))}
    </div>
  );
}
