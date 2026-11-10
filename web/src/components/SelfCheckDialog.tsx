import { useEffect, useState } from "react";
import { ArrowUpRight, Check, ShieldCheck } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useLocale } from "../i18n";
import { conversationPath } from "../lib/routes";
import { fetchModelCatalog, type ModelDescriptorWire } from "../lib/sessionApi";
import {
  fetchSelfCheck, fetchSelfCheckOptions, markCapabilitiesBound, startSelfCheck, type SelfCheckOptions, type SelfCheckRun,
} from "../lib/skillTreeApi";
import { Button, Modal, ModalTitle, Pill, Spinner, cream, inputStyle } from "./ui";

const POLL_MS = 2000;

/* 「Agent 自查」（2026-11-11）：像一个对话窗口。所选 Agent 只读复核它名单里还没定的配置，
   回答列出属于或疑似属于 Agent 绑定的条目，每条下面写理由；点条目名直接标为绑定。 */
export function SelfCheckDialog({ agentKey, agentLabel, onClose, onMarked }: {
  agentKey: string;
  agentLabel: string;
  onClose: () => void;
  onMarked: () => void;
}) {
  const { t } = useLocale();
  const navigate = useNavigate();
  const [options, setOptions] = useState<SelfCheckOptions | null>(null);
  const [models, setModels] = useState<ModelDescriptorWire[]>([]);
  const [model, setModel] = useState("");
  const [run, setRun] = useState<SelfCheckRun | null>(null);
  const [marked, setMarked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchSelfCheckOptions(agentKey, controller.signal).then(result => {
      setOptions(result);
      setModel(result.defaultModelId ?? "");
      return fetchModelCatalog(result.backendId, result.bindingId, controller.signal)
        .then(catalog => setModels(catalog.models.filter(m => m.availability !== "unavailable")))
        .catch(() => setModels([]));
    }).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure)); });
    return () => controller.abort();
  }, [agentKey]);

  const running = run?.status === "running";
  useEffect(() => {
    if (!run || !running) return;
    const timer = window.setTimeout(() => {
      fetchSelfCheck(run.conversationId).then(setRun).catch(failure => setError(failure instanceof Error ? failure.message : String(failure)));
    }, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [run, running]);

  const start = async () => {
    setBusy(true); setError(null);
    try { setRun(await startSelfCheck(agentKey, model || undefined)); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(false); }
  };
  const mark = async (id: string) => {
    setBusy(true); setError(null);
    try { await markCapabilitiesBound(agentKey, [id]); setMarked(old => new Set(old).add(id)); onMarked(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(false); }
  };
  const openConversation = () => { if (run) { onClose(); navigate(conversationPath(run.conversationId)); } };
  const name = (item: { kind: string; name: string | null }) => item.kind === "memory" ? t("import.nativeMemory") : item.name ?? "";

  return <Modal onClose={onClose} width={620}>
    <ModalTitle><span className="inline-flex items-center gap-2"><ShieldCheck size={18} />{t("selfCheck.title")} · {agentLabel}</span></ModalTitle>
    <p className="mt-1 text-xs leading-relaxed" style={{ color: cream(58) }}>{t("selfCheck.explain")}</p>
    <div className="mt-3 flex items-center gap-2">
      <select aria-label={t("selfCheck.model")} value={model} disabled={!options || busy || running}
        onChange={event => setModel(event.target.value)} style={{ ...inputStyle(), flex: 1 }}>
        <option value="">{t("selfCheck.defaultModel")}</option>
        {models.map(item => <option key={item.modelId} value={item.modelId}>{item.displayName ?? item.modelId}</option>)}
      </select>
      <Button variant="primary" disabled={!options || !options.count || busy || running} onClick={() => void start()}>
        {run ? t("selfCheck.again") : t("selfCheck.start")}
      </Button>
    </div>
    {options && <p className="mt-1 text-xs" style={{ color: cream(45) }}>
      {options.count ? t("selfCheck.scope", { count: options.count }) : t("selfCheck.nothing")}
    </p>}

    <div className="mt-4 min-h-32 rounded-md p-3 text-sm" data-testid="self-check-answer"
      style={{ border: "1px solid var(--line, rgba(255,255,255,.08))" }}>
      {!options && !error && <p className="flex items-center gap-2 text-xs" style={{ color: cream(50) }}><Spinner />{t("ui.loading")}</p>}
      {run?.status === "running" && <p role="status" className="flex items-center gap-2 text-xs" style={{ color: cream(58) }}>
        <Spinner />{t("selfCheck.running")}</p>}
      {run?.status === "failed" && <p role="alert" className="text-xs" style={{ color: "var(--danger)" }}>{t("selfCheck.failed")} · {run.error}</p>}
      {run?.status === "ended" && <p className="text-xs" style={{ color: cream(58) }}>{t("selfCheck.ended")}</p>}
      {run?.status === "answered" && <>
        {run.summary && <p className="mb-2 whitespace-pre-wrap">{run.summary}</p>}
        {!run.suggestions.length && <p className="text-xs" style={{ color: cream(58) }}>{t("selfCheck.noneBound")}</p>}
        <ul className="space-y-3">
          {run.suggestions.map(item => <li key={item.id}>
            <div className="flex items-center gap-2">
              {marked.has(item.id)
                ? <span className="inline-flex items-center gap-1 font-medium"><Check size={14} />{name(item)}</span>
                : <button type="button" className="font-medium underline-offset-2 hover:underline" disabled={busy}
                    title={t("selfCheck.markBound")} aria-label={t("selfCheck.markBoundOf", { name: name(item) })}
                    onClick={() => void mark(item.id)}>{name(item)}</button>}
              <Pill tone={item.verdict === "bound" ? "gold" : "muted"}>{t(`selfCheck.verdict.${item.verdict}`)}</Pill>
              {marked.has(item.id) && <span className="text-xs" style={{ color: cream(50) }}>{t("selfCheck.marked")}</span>}
            </div>
            <p className="mt-0.5 text-xs leading-relaxed" style={{ color: cream(62) }}>{item.reason}</p>
          </li>)}
        </ul>
      </>}
      {error && <p role="alert" className="text-xs" style={{ color: "var(--danger)" }}>{error}</p>}
    </div>
    <div className="mt-4 flex items-center gap-2">
      {run && <Button onClick={openConversation}><ArrowUpRight size={14} />{t("selfCheck.openConversation")}</Button>}
      <span className="flex-1" />
      <Button onClick={onClose}>{t("common.close")}</Button>
    </div>
  </Modal>;
}
