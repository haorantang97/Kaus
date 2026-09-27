/* 审批卡（DESIGN §6.2）：一句请求说明 + 按钮。
 *
 * 视觉：**边框加重，不换底色**；warn 状态竖条。
 * 按钮**完全来自事件里的 options**（`InteractionOption`）——引擎没给的选项就不画，
 * 站内不自造"本次会话都允许"这类选项（AD-71：缺的东西不渲染，不留占位）。
 * 认证请求（AD-08）与权限同形：`methods` 就是它的选项集合。
 */

import { useLocale } from "../../i18n";
import { Button } from "../ui";
import { cream } from "../ui";
import { CardShell, type EngineLabel } from "./CardShell";
import type { InteractionItem, InteractionOption } from "./timelineReducer";

/** 被判为"拒绝"的作答值。词表由各引擎自己给（常见的是 always/once/session/deny），
 *  认不出来就按中性的「已回答」走——不猜语义。 */
const DENIED: ReadonlySet<string> = new Set(["deny", "denied", "reject", "rejected", "no", "cancel"]);

/** 选项 kind → 按钮形态。引擎没给 kind 时一律用次级按钮。
 *  H-3：接受那一枚是"深底反白"（`accept`），不是绿底——会话工作区里没有强调色按钮。 */
function variantOf(option: InteractionOption): "accept" | "danger" | "ghost" {
  if (option.kind === "accept" || option.optionId === "allow") return "accept";
  if (option.kind === "reject" || option.optionId === "deny") return "danger";
  return "ghost";
}

export function PermissionCard({
  item,
  engine,
  time,
  onRespond,
}: {
  item: InteractionItem;
  engine?: EngineLabel;
  time?: string;
  onRespond?: (optionId: string) => void;
}) {
  const { t } = useLocale();
  const auth = item.interactionKind === "authentication";
  const title = auth
    ? (item.authentication?.message ?? t("card.authentication.fallbackTitle"))
    : (item.permission?.title ?? t("card.permission.fallbackTitle"));
  const detail = auth ? null : item.permission?.detail;
  const options: InteractionOption[] = auth
    ? (item.authentication?.methods ?? []).map((method) => ({ optionId: method, label: method }))
    : (item.permission?.options ?? []);
  const resolved = item.status === "resolved";
  const answer = item.decision ?? item.outcome;

  /* G-5：回答完就收成一行 `✓ 已允许 · <工具名>` / `✕ 已拒绝 · <工具名>`。
     "拒绝"只认引擎给的 decision 里的否定词；认不出来时按中性的「已回答」走，
     不猜一个语义（AD-71 的同一条精神：不确定就不编）。 */
  if (resolved) {
    const denied = DENIED.has(String(answer ?? "").toLowerCase());
    const subject = item.permission?.title ?? title;
    return (
      <div className="kaus-permission-done" data-testid="permission-resolved" data-decision={denied ? "denied" : "allowed"}>
        <span className={`kaus-permission-mark ${denied ? "is-denied" : ""}`}>{denied ? "✕" : "✓"}</span>
        <span>{answer ? t(denied ? "card.permission.denied" : "card.permission.allowed") : t("card.answered")}</span>
        <span className="kaus-permission-subject">{subject}</span>
      </div>
    );
  }

  return (
    <CardShell
      engine={engine}
      type={t(auth ? "card.type.authentication" : "card.type.permission")}
      time={time}
      status="warn"
      strongBorder
      /* 第 9 件：状态 chip 补齐（待回答），槽位与写法同推理行、工具行。 */
      right={
        <span className="text-[0.65rem]" style={{ color: cream(45) }}>
          {t("card.awaiting")}
        </span>
      }
    >
      <p className="text-sm leading-relaxed" style={{ color: "var(--midground-base)" }}>
        {title}
      </p>
      {detail && (
        <p className="mt-1 text-xs leading-relaxed" style={{ color: cream(55) }}>
          {detail}
        </p>
      )}
      {options.length > 0 && (
        /* G-5：按钮在卡内右下。 */
        <div className="mt-3 flex flex-wrap justify-end gap-2">
          {options.map((option) => {
            const variant = variantOf(option);
            return variant === "accept" ? (
              <button
                key={option.optionId}
                type="button"
                className="kaus-send"
                onClick={onRespond ? () => onRespond(option.optionId) : undefined}
              >
                {option.label}
              </button>
            ) : (
              <Button
                key={option.optionId}
                variant={variant}
                onClick={onRespond ? () => onRespond(option.optionId) : undefined}
              >
                {option.label}
              </Button>
            );
          })}
        </div>
      )}
    </CardShell>
  );
}
