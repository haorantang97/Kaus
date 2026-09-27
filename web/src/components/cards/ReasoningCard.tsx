/* 推理行（★ 定稿 G-4）：折叠态一行 `▸ 思考了 4s`，展开才是推理文本。
 *
 * 两个来源合用一行：
 *   - `reasoning.delta` 累积到某条消息的思考区（AD-27，MessageItem.reasoningChunks）；
 *   - `reasoning.status` 的 run 粒度状态/摘要（ReasoningItem）。
 * 引擎只给了其中一半时，另一半就是空——空的部分不渲染，不写"本引擎不提供"。
 *
 * `seconds` 由调用方给：公共信封里没有"思考耗时"，内核 item 也不存时间戳，
 * 页面按信封时间自己算（拿不到就只写"思考"）。
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { useLocale } from "../../i18n";

export function ReasoningCard({
  text,
  status,
  summary,
  seconds,
  running = false,
  defaultCollapsed = true,
}: {
  text?: string;
  status?: string;
  summary?: string | null;
  seconds?: number;
  /** 这一轮还在跑：折叠态换成 `● 正在思考…`（复用现有 pulse 点）。 */
  running?: boolean;
  defaultCollapsed?: boolean;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(!defaultCollapsed);
  const Chevron = open ? ChevronDown : ChevronRight;
  const label = running
    ? t("card.reasoning.running")
    : seconds !== undefined
      ? t("card.reasoning.rowSeconds", { seconds: Math.max(1, Math.round(seconds)) })
      : t("card.reasoning.row");
  const hasBody = Boolean(summary) || Boolean(text);

  return (
    <div className="kaus-reasoning" data-testid="reasoning-row">
      <button
        type="button"
        className="kaus-tool-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {running ? (
          <span className="kaus-run-dot" />
        ) : (
          <Chevron className="size-3 kaus-tool-chevron" />
        )}
        <span className="kaus-tool-name">{label}</span>
        {/* batch40（DESIGN ★L 第 5 条）：折起来又已经想完的那一行，行尾那个 `thinking`
            是废字——「思考了 4s」已经把这件事说完了。展开后照旧显示。 */}
        {status && !running && open && <span className="kaus-tool-tail">{status}</span>}
      </button>
      {open && hasBody && (
        <div className="kaus-reasoning-body">
          {summary && <p className="kaus-reasoning-summary">{summary}</p>}
          {text && <p className="kaus-reasoning-text">{text}</p>}
        </div>
      )}
    </div>
  );
}
