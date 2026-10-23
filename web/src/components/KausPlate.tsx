/* 铭牌（DESIGN ★M-1）。
 *
 * 原来是概览页的页头。**2026-09-13 用户裁决：概览页取消**（「没看懂——它展示的
 * 东西侧栏已经有了」），`/` 改渲染新会话草稿页；铭牌是那一页上唯一值得留下的
 * 东西，于是抽成这一枚组件，搬到草稿页最上方。
 *
 * 搬家时去掉了原来右侧那枚「新会话」按钮：**这一页本身就是新会话**，按钮点了
 * 等于原地不动——★L 的兜底规则（说不清它回答什么就不要）。
 *
 * 副行仍旧载真数据：`N 条等你处理 · N 条运行中 · N 个项目`，口径与
 * `buildAttentionItems` 共用 `plateCounts`（两处对不上，用户只会当界面在骗人）。
 * 为零的那一段不写；项目数总在，否则牌子看着像缺了半截。
 *
 * 样式（深色小岛、斜切 -7° 的绿字标、米白副行与继承来的投影）一个像素没动，
 * 仍旧是 `.kaus-overview-plate` 那一套。
 */

import { useMemo } from "react";

import { useLocale } from "../i18n";
import { plateCounts, type SidebarGroup } from "../lib/conversationIndex";

export interface KausPlateProps {
  groups: SidebarGroup[];
  /** 项目总数（空项目不在会话索引里，所以由调用方给）。 */
  projectCount: number;
}

export function KausPlate({ groups, projectCount }: KausPlateProps) {
  const { t } = useLocale();
  const summary = useMemo(() => {
    const counts = plateCounts(groups, projectCount);
    const parts: string[] = [];
    if (counts.attention > 0) parts.push(t("overview.plate.attention", { count: counts.attention }));
    if (counts.running > 0) parts.push(t("overview.plate.running", { count: counts.running }));
    parts.push(t("overview.plate.projects", { count: counts.projects }));
    return parts.join(" · ");
  }, [groups, projectCount, t]);

  return (
    <header className="kaus-overview-plate" data-testid="overview-plate">
      <span className="kaus-overview-stamp">
        KAUS
        <small data-testid="overview-plate-summary">{summary}</small>
      </span>
    </header>
  );
}
