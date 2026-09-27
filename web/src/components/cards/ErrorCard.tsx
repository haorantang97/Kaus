/* 报错 / 诊断卡（DESIGN §6.2）：一行摘要 + 可展开的原始错误（等宽）+ 可选重试。
   状态用左侧细竖条表达：error 一档给失败，warn 给 `diagnostic.notice` 的警告级。 */

import { useState } from "react";

import { useLocale } from "../../i18n";
import { Button, cream } from "../ui";
import { CardShell, MonoBlock, type CardStatus, type EngineLabel } from "./CardShell";

/** 诊断级别 → 竖条颜色。未知级别按 info 处理（不假装是错误）。 */
export function toneForLevel(level: string): Exclude<CardStatus, "none"> {
  const normalized = level.toLowerCase();
  if (normalized === "error" || normalized === "critical" || normalized === "fatal") return "error";
  if (normalized === "warning" || normalized === "warn") return "warn";
  return "info";
}

export function ErrorCard({
  summary,
  detail,
  code,
  tone = "error",
  engine,
  time,
  onRetry,
}: {
  summary: string;
  /** 原始错误 / 结构化 detail：默认折叠，展开是等宽块。 */
  detail?: string | null;
  code?: string | null;
  tone?: Exclude<CardStatus, "none">;
  engine?: EngineLabel;
  time?: string;
  /** 引擎报了可重试才传；不可重试就不画按钮。 */
  onRetry?: () => void;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  return (
    <CardShell
      engine={engine}
      type={t(tone === "error" ? "card.type.error" : tone === "warn" ? "card.type.warn" : "card.type.info")}
      time={time}
      status={tone}
    >
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 text-sm leading-relaxed" style={{ color: "var(--midground-base)" }}>
          {summary}
          {code && (
            <span className="ml-2 text-[0.65rem]" style={{ color: cream(45) }}>
              {code}
            </span>
          )}
        </p>
        {onRetry && <Button onClick={onRetry}>{t("card.error.retry")}</Button>}
      </div>
      {detail && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            className="text-[0.65rem] underline-offset-2 hover:underline"
            style={{ color: cream(45) }}
          >
            {open ? t("card.error.hideDetail") : t("card.error.showDetail")}
          </button>
          {open && (
            <div className="mt-1">
              <MonoBlock>{detail}</MonoBlock>
            </div>
          )}
        </div>
      )}
    </CardShell>
  );
}
