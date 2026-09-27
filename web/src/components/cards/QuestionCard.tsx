/* 提问卡（DESIGN §6.2 + ★ 定稿 G-5）：问题文字 + 选项或输入 + 提交按钮。
 *
 * 批次十三：提问与审批同为"常展开卡"（边框加重，不换底色），**回答后同样收成一行**
 * `✓ 已回答 · <问题前 20 字>`——和审批的收行用同一套 class。
 * 选项与"能不能自由输入"都来自事件（`allowFreeText`）；引擎没给的一律不画。 */

import { useState } from "react";

import { useLocale } from "../../i18n";
import { Button, cream, inputStyle } from "../ui";
import { CardShell, type EngineLabel } from "./CardShell";
import type { InteractionItem } from "./timelineReducer";

/** 收行时显示的问题摘要：前 20 字，超了加省略号。 */
export function questionDigest(prompt: string): string {
  const line = prompt.replace(/\s+/g, " ").trim();
  return line.length > 20 ? `${line.slice(0, 20)}…` : line;
}

export function QuestionCard({
  item,
  engine,
  time,
  onRespond,
}: {
  item: InteractionItem;
  engine?: EngineLabel;
  time?: string;
  /** optionId 为空表示自由文本作答。 */
  onRespond?: (answer: { optionId?: string; optionIds?: string[]; text?: string; cancelled?: boolean }) => void;
}) {
  const { t } = useLocale();
  const [text, setText] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const request = item.question;
  const resolved = item.status === "resolved";
  const prompt = request?.prompt ?? t("card.question.fallback");

  /* G-5：回答后收成一行（与审批卡同一套 class，不新造样式）。 */
  if (resolved) {
    return (
      <div className="kaus-permission-done" data-testid="question-resolved">
        <span className="kaus-permission-mark">✓</span>
        <span>{t("card.question.answered", { prompt: questionDigest(prompt) })}</span>
      </div>
    );
  }

  return (
    <CardShell engine={engine} type={t("card.type.question")} time={time} strongBorder>
      <p className="text-sm leading-relaxed" style={{ color: "var(--midground-base)" }}>
        {prompt}
      </p>
      {(request?.options.length ?? 0) > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {request?.options.map((option) => (
            <Button
              key={option.optionId}
              variant={request?.allowMultiple && selected.includes(option.optionId) ? "primary" : "ghost"}
              aria-pressed={request?.allowMultiple ? selected.includes(option.optionId) : undefined}
              disabled={!onRespond}
              onClick={onRespond ? () => request?.allowMultiple
                ? setSelected(previous => previous.includes(option.optionId) ? previous.filter(id => id !== option.optionId) : [...previous, option.optionId])
                : onRespond({ optionId: option.optionId }) : undefined}
            >
              {option.label}
            </Button>
          ))}
        </div>
      )}
      {request?.allowMultiple && (
        <Button disabled={!onRespond || selected.length === 0} onClick={() => onRespond?.({ optionIds: selected })}>
          {t("card.question.submit")}
        </Button>
      )}
      {request?.allowFreeText && (
        <div className="mt-3 flex items-center gap-2">
          <input
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={t("card.question.freeText")}
            style={inputStyle()}
          />
          {/* H-3：主按钮是"深底反白"，不是绿底（会话工作区里没有强调色按钮）。 */}
          <button
            type="button"
            className="kaus-send"
            disabled={!onRespond || !text.trim()}
            onClick={onRespond ? () => onRespond({ text: text.trim() }) : undefined}
          >
            {t("card.question.submit")}
          </button>
        </div>
      )}
      {onRespond && <div className="mt-2"><Button onClick={() => onRespond({ cancelled: true })}>{t("common.cancel")}</Button></div>}
    </CardShell>
  );
}
