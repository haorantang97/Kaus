import { useEffect, useState } from "react";
import { ChevronRight } from "lucide-react";
import { useLocale } from "../i18n";
import { fetchImportAgents, type ImportAgent } from "../lib/skillTreeApi";
import { CapabilityImportList } from "./CapabilityImportList";
import { EngineIcon } from "./EngineIcon";
import { Modal, ModalTitle, Spinner, cream } from "./ui";

/* 「导入能力」：随时找回接入时没导入的（后悔药）。先选 Agent，再出同一份勾选名单。 */
export function CapabilityImportDialog({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const { t } = useLocale();
  const [agents, setAgents] = useState<ImportAgent[] | null>(null);
  const [agent, setAgent] = useState<ImportAgent | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    fetchImportAgents(controller.signal).then(result => setAgents(result.agents))
      .catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure)); });
    return () => controller.abort();
  }, []);
  return <Modal onClose={onClose} width={560}>
    <ModalTitle>{agent ? <span className="inline-flex items-center gap-2"><EngineIcon engine={agent.agentKey} size={18} />{agent.agentLabel} · {t("import.title")}</span> : t("import.title")}</ModalTitle>
    {agent ? <CapabilityImportList agentKey={agent.agentKey} variant="later" onCancel={() => setAgent(null)} onImported={onImported} />
      : error ? <p role="alert" className="mt-2 text-xs" style={{ color: "var(--danger)" }}>{error}</p>
      : !agents ? <p className="flex items-center gap-2 py-6 text-xs" style={{ color: cream(50) }}><Spinner />{t("ui.loading")}</p>
      : !agents.length ? <p className="py-6 text-center text-xs" style={{ color: cream(50) }}>{t("import.noAgents")}</p>
      : <ul className="mt-2">{agents.map(row => <li key={row.agentKey}>
          <button type="button" className="flex w-full items-center gap-2 rounded px-2 py-2 text-left text-sm hover:bg-white/5" onClick={() => setAgent(row)}>
            <EngineIcon engine={row.agentKey} size={17} />
            <span className="flex-1">{row.agentLabel}</span>
            <span className="text-xs" style={{ color: cream(50) }}>{t("import.availableCount", { count: row.available })}</span>
            <ChevronRight size={14} />
          </button>
        </li>)}</ul>}
  </Modal>;
}
