import { useEffect, useState } from "react";
import { Activity, AlertTriangle, Clock } from "lucide-react";
import { apiGet } from "../lib/api";
import { t, useLocale } from "../i18n";
import { AsyncState, Card, cream, HAIR, Modal, Overlay, Pill, toast } from "./ui";
import { DriftAlert } from "./DriftAlert";
import { UpdateHealthAlert } from "./UpdateHealthAlert";

interface Health {
  agent_total: number; main: number; twin: number; sub: number; drafts: number;
  killed: number; effective_killed: number; lint_hard_total: number; lint_soft_total: number;
  default_mcp_count: number; agents_with_mcp: number; const_subscribed: number;
}
interface Summary {
  health: Health;
  review: { killed: string[]; hard_lint: { profile: string; count: number; items: { where: string }[] }[]; drafts: string[] };
  recent_changes: { target: string; summary: string; when: string; ago: string }[];
  generated_at: string;
}
interface Lint { profiles: { name: string; hard: { where: string; msg: string }[]; soft: { where: string; msg: string }[] }[]; total_hard: number; total_soft: number; }

function Row({ label, value, tone, onClick }: { label: string; value: string; tone?: "ok" | "warn" | "bad"; onClick?: () => void }) {
  const color = tone === "bad" ? "var(--danger)" : tone === "warn" ? "var(--accent-text)" : cream(70);
  return (
    <div className={`flex items-center justify-between py-1.5 text-xs ${onClick ? "cursor-pointer" : ""}`} style={{ borderTop: HAIR }} onClick={onClick}>
      <span style={{ color: cream(50) }}>{label}</span>
      <span style={{ color }}>{value}{onClick && value !== "0" ? " ▸" : ""}</span>
    </div>
  );
}

export function DashboardOverlay({ onClose, onGoProfile, refreshKey = 0 }: { onClose: () => void; onGoProfile: (name: string) => void; refreshKey?: number }) {
  const { t } = useLocale();
  const [d, setD] = useState<Summary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [lint, setLint] = useState<Lint | null>(null);
  const load = () => { setD(null); setErr(null); apiGet<Summary>("/api/dashboard/summary").then(setD).catch((e) => setErr(String(e?.message ?? e))); };
  useEffect(() => { load(); }, [refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const openLint = () => apiGet<Lint>("/api/lint").then(setLint).catch((e) => toast(t("dash.lint.failed", { error: e instanceof Error ? e.message : String(e) }), "bad"));

  const review = d ? [
    ...d.review.killed.map((n) => ({ icon: "⛔", title: n, sub: t("dash.review.killed"), go: n })),
    ...d.review.hard_lint.map((it) => ({ icon: "⚠", title: it.profile, sub: t("dash.review.hardLint", { count: it.count }), go: it.profile })),
    ...d.review.drafts.map((n) => ({ icon: "○", title: n, sub: t("dash.review.draft"), go: n })),
  ] : [];

  return (
    <Overlay title={t("dash.title")} onClose={onClose} onReload={() => load()}>
      <AsyncState data={d} err={err}>{(d) => (
          <>
            <p className="mb-5 text-xs" style={{ color: cream(50) }}>{t("dash.sub", { at: d.generated_at })}</p>
            <UpdateHealthAlert onGoProfile={onGoProfile} />
            <DriftAlert />
            <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
              <Card title={t("dash.health.title")} icon={Activity}>
                <div className="mb-1 text-2xl font-semibold" style={{ color: "var(--accent-text)" }}>{d.health.agent_total}<span className="ml-2 text-xs" style={{ color: cream(45) }}>{t("dash.health.unit")}</span></div>
                <div className="mb-2 text-[0.7rem]" style={{ color: cream(45) }}>{t("dash.health.breakdown", { main: d.health.main, twin: d.health.twin, sub: d.health.sub })}{d.health.drafts ? t("dash.health.drafts", { count: d.health.drafts }) : ""}</div>
                <Row label={t("dash.row.disabled")} value={`${t("dash.row.disabledValue", { killed: d.health.killed })}${d.health.effective_killed > d.health.killed ? t("dash.row.disabledCascade", { count: d.health.effective_killed - d.health.killed }) : ""}`} tone={d.health.killed ? "warn" : "ok"} />
                <Row label={t("dash.row.hardLint")} value={String(d.health.lint_hard_total)} tone={d.health.lint_hard_total ? "bad" : "ok"} onClick={openLint} />
                <Row label={t("dash.row.softLint")} value={String(d.health.lint_soft_total)} tone={d.health.lint_soft_total ? "warn" : "ok"} onClick={openLint} />
                <Row label={t("dash.row.mcp")} value={t("dash.row.mcpCount", { count: d.health.default_mcp_count })} />
                <Row label={t("dash.row.mcpInherited")} value={`${d.health.agents_with_mcp} / ${d.health.agent_total - d.health.main}`} />
                <Row label={t("dash.row.constitution")} value={d.health.const_subscribed === 0 ? t("dash.constitution.off") : t("dash.constitution.on", { count: d.health.const_subscribed })} />
              </Card>

              <Card title={t("dash.review.title", { count: review.length })} icon={AlertTriangle}>
                {review.length === 0 ? <div className="py-6 text-center text-xs" style={{ color: cream(45) }}>{t("dash.review.empty")}</div>
                  : review.map((it, i) => (
                    <div key={i} className="cursor-pointer rounded-lg px-2.5 py-2" style={{ borderBottom: HAIR }} onClick={() => onGoProfile(it.go)}>
                      <div className="text-xs">{it.icon} {it.title}</div>
                      <div className="mt-0.5 text-[0.68rem]" style={{ color: cream(45) }}>{it.sub}</div>
                    </div>
                  ))}
              </Card>

              <Card title={t("dash.changes.title", { count: d.recent_changes.length })} icon={Clock}>
                {d.recent_changes.length === 0 ? <div className="py-6 text-center text-xs" style={{ color: cream(45) }}>{t("dash.changes.empty")}</div>
                  : d.recent_changes.map((c, i) => (
                    <div key={i} className="flex items-baseline gap-2.5 py-1.5 text-xs" style={{ borderTop: i ? HAIR : undefined }}>
                      <span className="shrink-0" style={{ color: cream(40) }}>{c.ago}</span>
                      <span className="truncate" title={`${c.when} · ${c.summary}`}>{c.target}</span>
                    </div>
                  ))}
              </Card>
            </div>
          </>
        )}</AsyncState>

      {lint && <LintModal lint={lint} onClose={() => setLint(null)} />}
    </Overlay>
  );
}

function LintModal({ lint, onClose }: { lint: Lint; onClose: () => void }) {
  const withIssues = lint.profiles.filter((p) => p.hard.length || p.soft.length);
  return (
    <Modal onClose={onClose} width={640}>
      <div className="mb-3 flex items-center gap-3">
        <h3 className="text-sm font-semibold uppercase tracking-[0.12em]">{t("dash.lint.title")}</h3>
        <Pill tone="danger">{t("dash.lint.hard", { count: lint.total_hard })}</Pill>
        <Pill tone="gold">{t("dash.lint.soft", { count: lint.total_soft })}</Pill>
      </div>
      {withIssues.length === 0 ? <div className="py-6 text-center text-sm" style={{ color: cream(45) }}>{t("dash.lint.clean")}</div>
        : withIssues.map((p) => (
          <div key={p.name} className="mb-3">
            <div className="mb-1 text-xs font-semibold" style={{ color: "var(--accent-text)" }}>{p.name}</div>
            {p.hard.map((v, i) => <div key={`h${i}`} className="py-0.5 pl-3 text-[0.7rem]" style={{ color: "var(--danger)" }}>• {v.msg} <span style={{ color: cream(40) }}>({v.where.split("/").slice(-2).join("/")})</span></div>)}
            {p.soft.map((v, i) => <div key={`s${i}`} className="py-0.5 pl-3 text-[0.7rem]" style={{ color: cream(55) }}>· {v.msg} <span style={{ color: cream(40) }}>({v.where.split("/").slice(-2).join("/")})</span></div>)}
          </div>
        ))}
    </Modal>
  );
}
