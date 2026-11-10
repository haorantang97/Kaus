import { useCallback, useEffect, useState } from "react";
import { FileText, Plug, Puzzle, RotateCcw } from "lucide-react";
import { useLocale } from "../i18n";
import { fetchCapabilityChanges, ignoreCapabilityChanges, importCapabilities, markCapabilityChangesSeen, syncCapability,
  type AgentChanges, type ChangeItem } from "../lib/skillTreeApi";
import { EngineIcon } from "./EngineIcon";
import { Button, Modal, ModalTitle, Spinner, cream } from "./ui";

/* 各 Agent 那边的能力变化（新增、移除、有更新），按 Agent 分组（设计稿 capability-import 第 5 节）。
   打开即视为已看过，入口上的黄点消失，不管有没有处理。 */
export function CapabilityChangesPanel({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const { t } = useLocale();
  const [agents, setAgents] = useState<AgentChanges[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try { setAgents((await fetchCapabilityChanges()).agents); await markCapabilityChangesSeen(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const act = async (work: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await work(); onChanged(); await load(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(false); }
  };
  const icon = (kind: ChangeItem["kind"]) => kind === "mcp" ? <Plug size={14} /> : kind === "plugin" ? <Puzzle size={14} /> : <FileText size={14} />;
  const row = (agent: AgentChanges, item: ChangeItem, kind: "added" | "removed" | "updated") =>
    <li key={kind + item.id} className="flex items-center gap-2 py-1 text-sm">
      <span style={{ color: cream(55) }}>{icon(item.kind)}</span>
      <span className="min-w-0 flex-1 truncate" title={item.description}>{item.name}</span>
      <span className="text-xs" style={{ color: cream(50) }}>{t(`changes.${kind}`)}</span>
      {kind === "added" && <Button disabled={busy} onClick={() => void act(() => importCapabilities(agent.agentKey, [item.id]))}>{t("changes.import")}</Button>}
      {kind === "updated" && <Button disabled={busy} onClick={() => void act(() => syncCapability(item.id))}>{t("changes.sync")}</Button>}
      <Button disabled={busy} onClick={() => void act(() => ignoreCapabilityChanges(agent.agentKey, [item.id]))}>
        {kind === "removed" ? t("changes.ack") : t("changes.ignore")}</Button>
    </li>;

  return <Modal onClose={onClose} width={560}>
    <div className="flex items-center gap-2">
      <ModalTitle>{t("changes.title")}</ModalTitle>
      <span className="flex-1" />
      <button className="skill-tree-icon" disabled={busy} title={t("changes.check")} aria-label={t("changes.check")} onClick={() => void load()}><RotateCcw size={15} /></button>
    </div>
    {error && <p role="alert" className="mt-2 text-xs" style={{ color: "var(--danger)" }}>{error}</p>}
    {!agents ? <p className="flex items-center gap-2 py-6 text-xs" style={{ color: cream(50) }}><Spinner />{t("ui.loading")}</p>
      : !agents.length ? <p className="py-6 text-center text-xs" style={{ color: cream(50) }}>{t("changes.none")}</p>
      : agents.map(agent => <section key={agent.agentKey} className="mt-3">
        <h4 className="flex items-center gap-2 text-xs font-medium" style={{ color: cream(70) }}><EngineIcon engine={agent.agentKey} size={15} />{agent.agentLabel}</h4>
        <ul className="mt-1">
          {agent.added.map(item => row(agent, item, "added"))}
          {agent.updated.map(item => row(agent, item, "updated"))}
          {agent.removed.map(item => row(agent, item, "removed"))}
        </ul>
      </section>)}
    <div className="mt-4 flex justify-end"><Button onClick={onClose}>{t("skillTree.dismiss")}</Button></div>
  </Modal>;
}
