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

export function ContextRing({ usage }: { usage: UsageSnapshot | null }) {
  const used = usage?.contextUsed, limit = usage?.contextWindow;
  if (used == null || limit == null || !Number.isFinite(used) || !Number.isFinite(limit) || used < 0 || limit <= 0) return null;
  const ratio = used / limit, percent = Math.round(ratio * 100);
  const label = `${t("card.usage.context", { used: formatCount(used), window: formatCount(limit) })} · ${percent}%`;
  return <span className={`kaus-context-ring${ratio >= .8 ? " is-warning" : ""}`} role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, percent)} aria-valuetext={label} title={label}>
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" strokeWidth="2" opacity=".15" /><circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" strokeWidth="2" pathLength="100" strokeDasharray="100" strokeDashoffset={100 - Math.min(100, percent)} transform="rotate(-90 10 10)" strokeLinecap="round" /></svg>
  </span>;
}

/* 用量 Pill（★ 定稿 G-8）：从消息流上方挪到页头右侧，折成一枚 `总 30.7k`。
 * 悬停（`title` + 浮层）展开 in / out / total。一个数都没有时整枚不渲染（AD-71）。 */
export function UsagePill({ usage }: { usage: UsageSnapshot | null }) {
  const dash = t("conversation.usage.unknown");
  const cell = (value: number | null | undefined) => (value == null ? dash : formatCount(value));
  const total = usage?.totalTokens ?? null;
  if (
    usage == null ||
    (usage.totalTokens == null && usage.inputTokens == null && usage.outputTokens == null && (usage.contextUsed == null || !usage.contextWindow))
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
      <ContextRing usage={usage} />
      {(total != null || usage.outputTokens != null || usage.inputTokens != null) && <Pill tone="muted" title={detail}>
        {t("conversation.usage.pill", { value: cell(total ?? usage.outputTokens) })}
      </Pill>}
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
      <ContextRing usage={usage} />
      {parts.map((part) => (
        <span key={part}>{part}</span>
      ))}
    </div>
  );
}
