/* D-03: Project capabilities that did not reach this session. One neutral line,
   names and reason in the tooltip, and a link to the Project page where the
   capabilities can be applied. */

import { ArrowUpRight } from "lucide-react";

import { useLocale, type DictKey } from "../../i18n";
import { projectPath } from "../../lib/routes";
import { cream } from "../ui";

interface UnappliedItem {
  kind: string;
  names: string[];
  reason: string;
}

const KIND_KEYS: Record<string, DictKey> = {
  instructions: "timeline.projectUnapplied.instructions",
  mcp: "timeline.projectUnapplied.mcp",
  skills: "timeline.projectUnapplied.skills",
  project: "timeline.projectUnapplied.project",
};

/* Known reason codes; an unknown code is kept verbatim as the diagnostic. */
const REASON_KEYS: Record<string, DictKey> = {
  not_materialized: "timeline.projectUnapplied.reason.not_materialized",
  projection_failed: "timeline.projectUnapplied.reason.projection_failed",
  unsupported: "timeline.projectUnapplied.reason.unsupported",
  credential_bearing: "materialize.reason.credential_bearing",
  not_mapped: "materialize.reason.not_mapped",
  not_blockable: "materialize.reason.not_blockable",
  factory_protected: "materialize.reason.factory_protected",
  no_workspace_root: "materialize.reason.no_workspace_root",
  workspace_shared: "materialize.reason.workspace_shared",
  no_convention: "materialize.reason.no_convention",
  workspace_denied: "materialize.reason.workspace_denied",
  invalid_config: "materialize.reason.invalid_config",
  target_linked: "materialize.reason.target_linked",
  workspace_in_use: "timeline.projectUnapplied.reason.workspace_in_use",
  native_not_isolated: "timeline.projectUnapplied.reason.native_not_isolated",
};

export function projectUnappliedItems(data: unknown): UnappliedItem[] {
  const items = data && typeof data === "object" ? (data as { items?: unknown }).items : null;
  if (!Array.isArray(items)) return [];
  return items.flatMap((raw) => {
    if (!raw || typeof raw !== "object") return [];
    const { kind, names, reason } = raw as { kind?: unknown; names?: unknown; reason?: unknown };
    if (typeof kind !== "string") return [];
    return [{
      kind,
      names: Array.isArray(names) ? names.filter((name): name is string => typeof name === "string") : [],
      reason: typeof reason === "string" ? reason : "",
    }];
  });
}

export function ProjectUnappliedNotice({ data, time }: { data: unknown; time?: string }) {
  const { t } = useLocale();
  const items = projectUnappliedItems(data);
  if (!items.length) return null;
  const projectId = data && typeof data === "object" ? (data as { projectId?: unknown }).projectId : null;
  const labels = [...new Set(items.map((item) => t(KIND_KEYS[item.kind] ?? "timeline.projectUnapplied.project")))];
  const text = t("timeline.projectUnapplied", { items: labels.join(t("timeline.projectUnapplied.separator")) });
  const detail = items
    .map((item) => [
      t(KIND_KEYS[item.kind] ?? "timeline.projectUnapplied.project"),
      item.names.join(", "),
      item.reason ? (REASON_KEYS[item.reason] ? t(REASON_KEYS[item.reason]) : item.reason) : "",
    ].filter(Boolean).join(" · "))
    .filter(Boolean)
    .join("\n");
  return (
    <div className="flex items-center gap-2 px-1 py-0.5 text-[0.65rem]" style={{ color: cream(40) }} data-testid="project-unapplied">
      <span className="h-px flex-1" style={{ background: cream(11) }} />
      <span title={detail || undefined}>{text}</span>
      {typeof projectId === "string" && projectId && (
        <a
          href={projectPath(projectId)}
          className="inline-flex items-center"
          aria-label={t("timeline.projectUnapplied.open")}
          title={t("timeline.projectUnapplied.open")}
        >
          <ArrowUpRight size={12} aria-hidden />
        </a>
      )}
      {time && <span className="tabular-nums">{time}</span>}
      <span className="h-px flex-1" style={{ background: cream(11) }} />
    </div>
  );
}
