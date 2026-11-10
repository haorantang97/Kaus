import { useEffect, useMemo, useState } from "react";
import { Brain, ChevronRight, FileText, Plug, Puzzle } from "lucide-react";
import { useLocale } from "../i18n";
import { fetchImportCandidates, importCapabilities, skipCapabilityImport, type ImportCandidate } from "../lib/skillTreeApi";
import { Button, Spinner, confirmAsync, cream } from "./ui";

/* 导入 Agent 原有能力的勾选名单（设计稿 capability-import 第 4 节）。
   接入时出现一次；之后从技能树的「导入能力」随时找回没导入的。勾了才进技能树，不在树上的不加载。
   Agent 绑定能力置灰、固定给它自己用；别的 Agent 已导入的同一项显示「已在技能树中」，导入时合并。
   「Agent 自带记忆」（2026-11-11）：勾选 = 已有记忆复制进项目记忆；勾不勾开对话时都关掉它自带的那份；
   关不掉的置灰并附说明。 */
export function CapabilityImportList({ agentKey, variant, onImported, onSkipped, onCancel, footerStart }: {
  agentKey: string;
  variant: "connect" | "later";
  onImported: () => void;
  onSkipped?: () => void;
  onCancel?: () => void;
  footerStart?: React.ReactNode;
}) {
  const { t } = useLocale();
  const [items, setItems] = useState<ImportCandidate[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchImportCandidates(agentKey, controller.signal).then(result => {
      setItems(result.items);
      // 接入时默认全选可导入的；之后找回时只预选已经在树上的（旧规则下安排过的），其余让用户挑。
      setChosen(new Set(result.items.filter(i => i.state === "available" && (variant === "connect" || i.assigned)).map(i => i.id)));
    }).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure)); });
    return () => controller.abort();
  }, [agentKey, variant]);

  const importable = useMemo(() => (items ?? []).filter(i => i.state === "available" && i.available !== false), [items]);
  const merging = useMemo(() => (items ?? []).filter(i => i.state === "inTree"), [items]);
  const allChosen = importable.length > 0 && importable.every(i => chosen.has(i.id));
  const flip = (old: Set<string>, id: string) => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; };
  const toggle = (id: string) => setChosen(old => flip(old, id));
  const toggleOpen = (id: string) => setOpen(old => flip(old, id));

  const submit = async () => {
    // 接入时一项都不选，等于跳过：同样要确认「以空白状态接入」。
    if (variant === "connect" && !chosen.size && !merging.length) return skip();
    setBusy(true); setError(null);
    try { await importCapabilities(agentKey, [...chosen, ...merging.map(i => i.id)]); onImported(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t("import.failed")); }
    finally { setBusy(false); }
  };
  const skip = async () => {
    if (!await confirmAsync(t("import.skipConfirm"), { okLabel: t("import.skipOk") })) return;
    setBusy(true); setError(null);
    try { await skipCapabilityImport(agentKey); onSkipped?.(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t("import.failed")); }
    finally { setBusy(false); }
  };

  const icon = (kind: ImportCandidate["kind"]) => kind === "mcp" ? <Plug size={15} /> : kind === "plugin" ? <Puzzle size={15} />
    : kind === "memory" ? <Brain size={15} /> : <FileText size={15} />;
  const memoryLabel = (item: ImportCandidate) => item.state === "bound" ? t("import.memoryLocked")
    : item.state === "imported" ? t("import.memoryCopied", { count: item.memory?.copied ?? 0 })
    : item.state === "closed" ? (item.memory?.readable ? t("import.memoryEmpty") : t("import.memoryUnreadable"))
    : t("import.memoryCount", { count: item.memory?.count ?? 0 });
  const label = (item: ImportCandidate) => item.kind === "memory" ? memoryLabel(item)
    : item.state === "bound" ? t("import.bound") : item.state === "inTree" ? t("import.inTree")
    : item.state === "imported" ? t("import.imported") : item.available === false ? t("skillTree.sourceUnavailable") : "";
  const name = (item: ImportCandidate) => item.kind === "memory" ? t("import.nativeMemory") : item.name;

  if (!items) return <p role="status" className="flex min-h-40 items-center justify-center gap-2 text-xs" style={{ color: cream(50) }}>
    {error ?? <><Spinner />{t("ui.loading")}</>}</p>;

  return <div data-testid="capability-import-list">
    <div className="mb-2 flex items-center gap-2 text-xs" style={{ color: cream(62) }}>
      <label className="inline-flex items-center gap-1.5">
        <input type="checkbox" checked={allChosen} disabled={!importable.length || busy}
          onChange={() => setChosen(allChosen ? new Set() : new Set(importable.map(i => i.id)))} />
        {t("import.selectAll")}
      </label>
      <span className="flex-1" />
      <span>{t("import.chosen", { count: chosen.size })}</span>
    </div>
    <ul className="max-h-[48vh] overflow-auto rounded-md" style={{ border: "1px solid var(--line, rgba(255,255,255,.08))" }}>
      {!items.length && <li className="px-3 py-6 text-center text-xs" style={{ color: cream(50) }}>{t("import.none")}</li>}
      {items.map(item => {
        const fixed = item.state !== "available" || item.available === false;
        const checked = item.kind === "memory" ? item.state === "imported" || chosen.has(item.id)
          : item.state === "bound" || item.state === "inTree" || item.state === "imported" || chosen.has(item.id);
        const expanded = open.has(item.id);
        return <li key={item.id} className="px-3 py-1.5 text-sm" style={{ borderTop: "1px solid var(--line, rgba(255,255,255,.06))", opacity: fixed && item.state !== "inTree" ? 0.62 : 1 }}>
          <div className="flex items-center gap-2">
            <input type="checkbox" aria-label={name(item)} checked={checked} disabled={fixed || busy} onChange={() => toggle(item.id)} />
            <span style={{ color: cream(58) }}>{icon(item.kind)}</span>
            <span className="min-w-0 flex-1 truncate">{name(item)}</span>
            {label(item) && <span className="shrink-0 text-xs" style={{ color: cream(50) }}>{label(item)}</span>}
            <button type="button" className="skill-tree-icon" aria-expanded={expanded} title={t("import.details")} aria-label={t("import.detailsOf", { name: name(item) })}
              onClick={() => toggleOpen(item.id)}><ChevronRight size={14} className={expanded ? "is-open" : ""} style={{ transform: expanded ? "rotate(90deg)" : undefined }} /></button>
          </div>
          {item.kind === "memory" && item.state === "bound" && <p className="mt-0.5 pl-7 text-xs" style={{ color: cream(50) }}>{t("import.memoryLockedNote")}</p>}
          {expanded && <div className="mt-1 pl-7 text-xs" style={{ color: cream(58) }}>
            <p>{item.kind === "memory" ? t("import.memoryDescription") : item.description || t("import.noDescription")}</p>
            {item.sourcePath && <p className="mt-0.5 break-all" style={{ color: cream(42) }}>{t("import.source")} · {item.sourcePath}</p>}
          </div>}
        </li>;
      })}
    </ul>
    {error && <p role="alert" className="mt-2 text-xs" style={{ color: "var(--danger)" }}>{error}</p>}
    <div className="mt-4 flex items-center gap-2">
      {footerStart}
      <span className="flex-1" />
      {variant === "connect"
        ? <Button disabled={busy} onClick={() => void skip()}>{t("import.skip")}</Button>
        : <Button disabled={busy} onClick={onCancel}>{t("import.cancel")}</Button>}
      <Button variant="primary" disabled={busy || (!chosen.size && !merging.length && variant === "later")} onClick={() => void submit()}>
        {chosen.size || merging.length ? t("import.submit", { count: chosen.size + merging.length }) : t("import.continue")}
      </Button>
    </div>
  </div>;
}
