/* Startup placeholder while the local session connection is established. */

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
