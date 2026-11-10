/* 项目页「记忆」（设计稿 2026-11-02 第 5 节）。
 *
 * 记忆由 Agent 在对话里静默维护（记忆 MCP）；这里只做查看、修改、删除，外加按条撤销的历史，
 * 不设整理或审批入口——想整理就在任意对话里说「整理一下项目记忆」。上级 Project 的条目只读。
 * 项目概述固定在最上面，可以直接改写。还没有时，开对话注入的索引会先用新建项目时填的描述顶上。 */

import { useCallback, useEffect, useState } from "react";
import { Brain, History, Undo2 } from "lucide-react";

import { useLocale } from "../i18n";
import { backendDisplay } from "../lib/backend-display";
import {
  createProjectMemory,
  deleteProjectMemory,
  describeFailure,
  fetchMemoryHistory,
  fetchProjectMemories,
  patchProjectMemory,
  undoMemoryChange,
  type MemoryHistoryWire,
  type MemoryWire,
} from "../lib/sessionApi";
import { relativeTime } from "../lib/shellPrefs";
import { CardRowMenu } from "./CardRowMenu";
import { Button, Card, Pill, cream, inputStyle, toast } from "./ui";

interface Draft { id: string | null; kind: "note" | "overview"; topic: string; body: string }

export function ProjectMemoryCard({ projectId, projectLabel }: {
  projectId: string;
  projectLabel: (projectId: string) => string;
}) {
  const { t, locale } = useLocale();
  const [memories, setMemories] = useState<MemoryWire[] | null>(null);
  const [history, setHistory] = useState<MemoryHistoryWire[] | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision(value => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    fetchProjectMemories(projectId, controller.signal)
      .then(result => setMemories(Array.isArray(result?.memories) ? result.memories : []))
      .catch(() => { if (!controller.signal.aborted) setMemories([]); });
    if (history !== null) {
      fetchMemoryHistory(projectId, controller.signal).then(result => setHistory(result.history ?? [])).catch(() => {});
    }
    return () => controller.abort();
  // eslint-disable-next-line react-hooks/exhaustive-deps -- history 只在打开时取，之后随 revision 刷新
  }, [projectId, revision]);

  const run = async (work: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try { await work(); reload(); toast(done, "ok"); return true; }
    catch (failure) { toast(t("memory.toast.failed", { error: describeFailure(failure) }), "bad"); return false; }
    finally { setBusy(false); }
  };

  const save = async () => {
    if (!draft || !draft.body.trim() || !draft.topic.trim()) return;
    const ok = await run(() => draft.id
      ? patchProjectMemory(projectId, draft.id, { topic: draft.topic, body: draft.body })
      : createProjectMemory(projectId, { topic: draft.topic, body: draft.body, kind: draft.kind }), t("memory.toast.saved"));
    if (ok) setDraft(null);
  };

  const toggleHistory = () => {
    if (history !== null) { setHistory(null); return; }
    fetchMemoryHistory(projectId).then(result => setHistory(result.history ?? [])).catch(() => setHistory([]));
  };

  const source = (value: string | null) => !value ? "" : value === "user" ? t("memory.source.user")
    : value === "kaus" ? t("memory.source.kaus")
    : value.startsWith("import:") ? t("memory.source.imported", { name: backendDisplay(value.slice(7)).displayName })
    : backendDisplay(value).displayName;
  const until = (item: MemoryWire) => item.type === "temporary" && item.expiresAt
    ? t("memory.temporaryUntil", { date: new Date(item.expiresAt).toLocaleDateString(locale, { month: "numeric", day: "numeric" }) })
    : null;
  const overview = memories?.find(item => item.kind === "overview" && item.projectId === projectId) ?? null;
  const notes = (memories ?? []).filter(item => item !== overview);
  const historyLabel = t("memory.history");

  const editor = draft && (
    <div className="kaus-instruction-editor">
      {draft.kind !== "overview" && <input value={draft.topic} onChange={e => setDraft({ ...draft, topic: e.target.value })}
        placeholder={t("memory.field.topic")} aria-label={t("memory.field.topic")} style={inputStyle()} />}
      <textarea value={draft.body} onChange={e => setDraft({ ...draft, body: e.target.value })} rows={4} autoFocus
        aria-label={draft.kind === "overview" ? t("memory.overview") : t("memory.field.body")}
        style={{ ...inputStyle(), resize: "vertical", lineHeight: 1.6 }}
        onKeyDown={e => { if (e.key === "Escape") setDraft(null); if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void save(); }} />
      <div className="flex justify-end gap-2">
        <Button onClick={() => setDraft(null)} disabled={busy}>{t("common.cancel")}</Button>
        <Button variant="primary" onClick={() => void save()} disabled={busy || !draft.body.trim()}>{t("common.save")}</Button>
      </div>
    </div>
  );

  const row = (item: MemoryWire) => {
    const own = item.projectId === projectId;
    if (draft?.id === item.id) return <li key={item.id} className="kaus-instruction">{editor}</li>;
    return (
      <li key={item.id} className="kaus-instruction">
        <div className="kaus-instruction-head">
          <span className="kaus-instruction-title">{item.topic}</span>
          {!own && <Pill tone="muted">{t("instructions.state.inherited", { name: projectLabel(item.projectId) })}</Pill>}
          {until(item) && <Pill tone="muted">{until(item)}</Pill>}
          <span className="kaus-memory-meta">{[source(item.source), relativeTime(item.updatedAt, locale)].filter(Boolean).join(" · ")}</span>
          <CardRowMenu label={t("memory.menu")} actions={own ? (["edit", "delete"] as const) : ([] as const)} busy={busy || draft !== null}
            actionLabel={action => t(`memory.action.${action}`)}
            onPick={action => action === "edit"
              ? setDraft({ id: item.id, kind: item.kind, topic: item.topic, body: item.body })
              : void run(() => deleteProjectMemory(projectId, item.id), t("memory.toast.deleted"))} />
        </div>
        <p className="kaus-instruction-body">{item.body}</p>
      </li>
    );
  };

  return (
    <Card title={t("memory.title")} icon={Brain} right={
      <Button aria-label={historyLabel} title={historyLabel} aria-pressed={history !== null} onClick={toggleHistory}><History size={15} /></Button>
    }>
      {memories === null ? <div className="py-2 text-xs" style={{ color: cream(45) }}>{t("instructions.loading")}</div> : (
        <ul className="kaus-instructions" data-testid="project-memory">
          {overview ? row(overview) : draft?.kind === "overview" ? <li className="kaus-instruction">{editor}</li> : (
            <li className="kaus-instruction">
              <div className="kaus-instruction-head">
                <span className="kaus-instruction-title">{t("memory.overview")}</span>
                <CardRowMenu label={t("memory.menu")} actions={["edit"] as const} busy={busy || draft !== null}
                  actionLabel={() => t("memory.action.edit")}
                  onPick={() => setDraft({ id: null, kind: "overview", topic: t("memory.overview"), body: "" })} />
              </div>
              <p className="kaus-instruction-body">{t("memory.overviewEmpty")}</p>
            </li>
          )}
          {notes.map(row)}
          {notes.length === 0 && <li className="py-1 text-xs" style={{ color: cream(45) }}>{t("memory.empty")}</li>}
        </ul>
      )}
      {history !== null && (
        <div className="kaus-instruction-delivery" data-testid="memory-history">
          <div className="kaus-instruction-delivery-title">{historyLabel}</div>
          {history.length === 0 && <span className="text-xs" style={{ color: cream(45) }}>{t("memory.historyEmpty")}</span>}
          {history.slice(0, 20).map(entry => (
            <div key={entry.id} className="kaus-instruction-delivery-head">
              <span className="kaus-instruction-delivery-agents">
                {t(`memory.change.${entry.action}`)} · {(entry.after ?? entry.before)?.topic} · {source(entry.actor)} · {relativeTime(entry.at, locale)}
              </span>
              <button type="button" className="kaus-cap-icon-button" disabled={busy} aria-label={t("memory.undo")} title={t("memory.undo")}
                onClick={() => void run(() => undoMemoryChange(projectId, entry.id), t("memory.toast.undone"))}><Undo2 size={14} /></button>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
