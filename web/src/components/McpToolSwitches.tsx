import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useLocale } from "../i18n";
import { fetchMcpTools, setMcpToolSwitches, type McpToolList, type SkillTreeSnapshot } from "../lib/skillTreeApi";
import { Spinner, cream } from "./ui";

/* MCP 单个工具开关（2026-11-11）：展开一个 MCP，按它的工具清单逐个开关。开关设在 MCP 本身，
   凡用到它的 Project 和 Agent 都按它执行；要登录或密钥才能列出清单的，先只能整体开关。
   以后搬进「MCP 管理」的详情（docs/product/mcp-management-design-20261111.md）。 */
export function McpToolSwitches({ skillId, disabled, onSnapshot }: {
  skillId: string;
  disabled: string[];
  onSnapshot: (next: SkillTreeSnapshot) => void;
}) {
  const { t } = useLocale();
  const [list, setList] = useState<McpToolList | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = (refresh: boolean, signal?: AbortSignal) => {
    setError(null);
    return fetchMcpTools(skillId, refresh, signal).then(setList)
      .catch(failure => { if (!signal?.aborted) setError(failure instanceof Error ? failure.message : String(failure)); });
  };
  useEffect(() => {
    const controller = new AbortController();
    void load(false, controller.signal);
    return () => controller.abort();
  }, [skillId]); // eslint-disable-line react-hooks/exhaustive-deps

  const flip = async (tool: string) => {
    const next = disabled.includes(tool) ? disabled.filter(name => name !== tool) : [...disabled, tool];
    setBusy(true); setError(null);
    try { onSnapshot(await setMcpToolSwitches(skillId, next)); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(false); }
  };
  const refresh = async () => { setBusy(true); await load(true); setBusy(false); };

  return <div className="skill-tree-tools" data-testid="mcp-tool-switches">
    <div className="flex items-center gap-2 text-xs" style={{ color: cream(50) }}>
      <span className="flex-1">{list?.status === "ok" ? t("mcpTools.count", { count: list.tools.length }) : t("mcpTools.title")}</span>
      <button type="button" className="skill-tree-icon" disabled={busy || !list} title={t("mcpTools.refresh")} aria-label={t("mcpTools.refresh")}
        onClick={() => void refresh()}><RefreshCw size={13} /></button>
    </div>
    {!list && !error && <p className="flex items-center gap-2 text-xs" style={{ color: cream(50) }}><Spinner />{t("ui.loading")}</p>}
    {list?.status === "auth_required" && <p className="text-xs" style={{ color: cream(55) }}>{t("mcpTools.authRequired")}</p>}
    {list?.status === "failed" && <p className="text-xs" style={{ color: cream(55) }}>{t("mcpTools.failed")}{list.error ? ` · ${list.error}` : ""}</p>}
    {list?.status === "ok" && <ul>
      {list.tools.map(tool => <li key={tool.name}>
        <label className="flex items-center gap-2 py-0.5 text-xs" title={tool.description || tool.name}>
          <input type="checkbox" checked={!disabled.includes(tool.name)} disabled={busy} onChange={() => void flip(tool.name)} />
          <span className="truncate">{tool.name}</span>
        </label>
      </li>)}
    </ul>}
    {error && <p role="alert" className="text-xs" style={{ color: "var(--danger)" }}>{error}</p>}
  </div>;
}
