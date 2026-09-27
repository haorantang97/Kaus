/* 外壳骨架（批次十六第 3 件 / 走查 F5b）。
 *
 * `useSessionHost()` 还在探测（`probing`）时，外壳**既不知道**该渲染新会话外壳
 * 还是旧外壳。以前这一刻渲染的是旧的宣传壳（HomeHero / 概览），于是直开
 * `/new`、`/vault`、`/projects/*` 都会先闪一屏和目标页毫无关系的画面——走查里
 * 「是否已到达」的判断因此失灵。
 *
 * 这里给的是**中性骨架**：一条侧栏骨架 + 主区三行 `.kaus-skeleton-line`。
 * 不写产品名、不写宣传语、不给任何按钮——什么都还没判定完，就什么都别承诺。
 * 复用会话页已有的骨架 class，不新造颜色与圆角（AD-78）。
 */

import { t } from "../i18n";

export function ShellSkeleton() {
  return (
    <div
      className="kaus-shell-skeleton"
      data-testid="shell-skeleton"
      role="status"
      aria-busy="true"
      aria-label={t("shell.loading")}
      style={{ background: "var(--background-base)" }}
    >
      <div className="kaus-shell-skeleton-rail">
        <div className="kaus-skeleton-line" />
        <div className="kaus-skeleton-line" />
        <div className="kaus-skeleton-line" />
      </div>
      <div className="kaus-shell-skeleton-main">
        <div className="kaus-skeleton-line" />
        <div className="kaus-skeleton-line" />
        <div className="kaus-skeleton-line" />
      </div>
    </div>
  );
}
