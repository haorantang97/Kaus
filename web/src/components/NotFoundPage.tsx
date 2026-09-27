/* 「没有这一页」（批次十一第 2 件）。
 *
 * 走查里最刺眼的两条：随便敲一个地址不报 404，而是渲染成概览；不存在的项目
 * `/projects/xxx` 也渲染出一张带写按钮的"幽灵页"（点下去会对着一个不存在的
 * 项目发写请求）。这里给一张极简页：大字 + 一句话 + 「回概览」。
 *
 * 排版沿用草稿页的 `kaus-draft-page` / `kaus-draft-window` / `kaus-draft-empty`
 * （AD-78：不新造样式），所以这张页在浅深两套主题下都跟着已有变量走。
 * 页面上**没有任何写操作**——这正是它存在的理由。
 */

import { Link } from "react-router-dom";

import { useLocale, type DictKey } from "../i18n";
import { cream } from "./ui";

export interface NotFoundPageProps {
  /** `page` = 地址不存在；`project` = 地址对但项目树里没有这个项目。 */
  kind?: "page" | "project";
  /** 出问题的地址/项目标识，小字显示，方便贴回给我们。 */
  detail?: string;
}

export function NotFoundPage({ kind = "page", detail }: NotFoundPageProps) {
  const { t } = useLocale();
  return (
    <div className="kaus-draft-page" data-testid="not-found">
      <div className="kaus-draft-window">
        <div className="kaus-draft-empty">
          <h1>{t(`notFound.${kind}.title` as DictKey)}</h1>
          <p style={{ color: cream(50) }}>{t(`notFound.${kind}.sub` as DictKey)}</p>
          {detail && (
            <p className="mt-1 text-[0.68rem]" style={{ color: cream(38) }}>
              <code>{detail}</code>
            </p>
          )}
          <p className="mt-3">
            <Link className="underline" style={{ color: "var(--accent-text)" }} to="/">
              {t("notFound.back")}
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
