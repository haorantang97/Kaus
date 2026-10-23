import { RotateCw } from "lucide-react";
import { useLocale } from "../i18n";
import type { SessionHostStatus } from "../lib/useSessionHost";
import { ShellSkeleton } from "./ShellSkeleton";

export function SessionHostGate({ status, onRetry }: {
  status: SessionHostStatus;
  onRetry: () => void;
}) {
  const { t } = useLocale();
  if (status === "probing") return <ShellSkeleton />;
  return (
    <div data-testid="session-host-gate" className="flex h-dvh w-full items-center justify-center" style={{ background: "var(--background-base)", color: "var(--midground-base)" }}>
      <div role="status" className="flex items-center gap-3 text-sm">
        <span>{t("shell.disconnected")}</span>
        <button type="button" className="kaus-button" onClick={onRetry} title={t("card.error.retry")} aria-label={t("card.error.retry")}>
          <RotateCw size={16} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
