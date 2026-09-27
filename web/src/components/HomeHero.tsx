/* 概览页（`/`）。
 *
 * 批次十一第 5 件：这页原来只有口号、没有一个动作——走查者站在这里不知道往哪走。
 * 现在底部多两枚按钮：「新会话」（→ `/new`）与「看项目」（→ `/agents`）。**其余不动**
 * （AD-78：字体/布局/图标/圆角一律不动）。
 *
 * 文案全部走词典（AD-96 / 批次十一第 4 件）。那几行大写英文是**字标与印刷元素**
 * （KAUS CONTROL PLANE / EST. 2026 / 大标题…），中英两列同值——翻译它们等于改设计。
 */

import type { OrgNode } from "../lib/api";
import { useLocale } from "../i18n";
import { dname } from "./ui";

export function HomeHero({
  agentCount,
  skillCount,
  depth,
  latest,
  onOpenAgent,
  onNewConversation,
  onBrowseProjects,
}: {
  agentCount: number | null;
  skillCount: number | null;
  depth: number | null;
  latest?: OrgNode;
  onOpenAgent: (name: string) => void;
  /** 有会话功能（flag 开）时才给；关闭时不渲染那枚按钮。 */
  onNewConversation?: () => void;
  onBrowseProjects: () => void;
}) {
  const { t } = useLocale();
  const nodeLabel = agentCount === null ? t("home.stat.loading") : t("home.stat.nodes", { count: agentCount });
  const depthLabel = depth === null ? t("home.stat.levelsUnknown") : t("home.stat.levels", { count: depth });
  const rootSkillLabel =
    skillCount === null ? t("home.stat.rootSkillsLoading") : t("home.stat.rootSkills", { count: skillCount });
  return (
    <section className="home-hero">
      <div className="home-hero-image" />
      <div className="home-hero-grade" />
      <div className="home-hero-frame" />
      <div className="home-grid-line home-grid-line-x home-grid-line-x-one" />
      <div className="home-grid-line home-grid-line-x home-grid-line-x-two" />
      <div className="home-grid-line home-grid-line-y" />

      <div className="home-tick home-tick-time">{t("home.stamp.plane")}</div>
      <div className="home-tick home-tick-year">{t("home.stamp.year")}</div>
      <div className="home-vertical home-vertical-left">{t("home.vertical.left")}</div>
      <div className="home-vertical home-vertical-right">{t("home.vertical.right")}</div>

      <div className="home-logo-stamp">
        KAUS
        <small>{t("home.logo.sub")}</small>
      </div>

      <h1 className="home-big-title">
        {t("home.title.line1")}
        <br />
        {t("home.title.line2")}
      </h1>

      {/* 下一步（第 5 件）：沿用导航项的 `.shell-nav-item` 形态，不新造按钮样式。 */}
      <div className="home-actions">
        {onNewConversation && (
          <button type="button" className="shell-nav-item is-active" onClick={onNewConversation}>
            <span className="shell-nav-label">{t("home.action.newConversation")}</span>
          </button>
        )}
        <button type="button" className="shell-nav-item" onClick={onBrowseProjects}>
          <span className="shell-nav-label">{t("home.action.projects")}</span>
        </button>
      </div>

      <div className="home-scale home-scale-left">{t("home.scale.left")}</div>
      <div className="home-scale home-scale-right">
        <span />
        {nodeLabel} · {depthLabel}
      </div>

      {latest && (
        <button type="button" className="home-hotspot" onClick={() => onOpenAgent(latest.name)}>
          <span className="home-hotspot-head">
            <span>{t("home.hotspot.view")}</span>
            <span>↗</span>
          </span>
          <span className="home-hotspot-body">
            <span className="home-hotspot-label">{t("home.hotspot.label", { skills: rootSkillLabel })}</span>
            <span className="home-hotspot-name">{dname(latest)}</span>
          </span>
        </button>
      )}
    </section>
  );
}
