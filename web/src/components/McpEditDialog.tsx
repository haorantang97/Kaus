import { useState } from "react";
import { Plus, X } from "lucide-react";
import { useLocale } from "../i18n";
import { addMcp, updateMcp, type McpDetail, type McpInput } from "../lib/mcpApi";
import { Button, Modal, ModalTitle, cream, inputStyle } from "./ui";

type Pair = { key: string; value: string };

/* 添加或编辑在 Kaus 里添加的 MCP（设计稿 mcp-management 第 3 节：第一步只支持手动填写）。
   环境变量值与请求头值按密钥处理：加密保存、从不回显；编辑时留空表示保持原值。 */
export function McpEditDialog({ editing, onClose, onSaved }: {
  editing?: McpDetail | null;
  onClose: () => void;
  onSaved: (saved: McpDetail) => void;
}) {
  const { t } = useLocale();
  const definition = editing?.definition;
  const [name, setName] = useState(editing?.name ?? "");
  const [description, setDescription] = useState(editing?.description ?? "");
  const [transport, setTransport] = useState<"stdio" | "http">(definition && definition.transport !== "stdio" ? "http" : "stdio");
  const [command, setCommand] = useState(definition?.command ?? "");
  const [args, setArgs] = useState((definition?.args ?? []).join("\n"));
  const [url, setUrl] = useState(definition?.url ?? "");
  const initial = (keys?: string[]) => (keys ?? []).map(key => ({ key, value: "" }));
  const [env, setEnv] = useState<Pair[]>(initial(definition?.env));
  const [headers, setHeaders] = useState<Pair[]>(initial(definition?.headers));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stored = new Set(definition?.valuesSet ?? []);

  const pairs = transport === "stdio" ? env : headers;
  const setPairs = transport === "stdio" ? setEnv : setHeaders;
  const edit = (index: number, change: Partial<Pair>) => setPairs(pairs.map((pair, i) => i === index ? { ...pair, ...change } : pair));
  const table = (rows: Pair[]) => Object.fromEntries(rows.filter(row => row.key.trim()).map(row => [row.key.trim(), row.value]));

  const submit = async () => {
    const input: McpInput = transport === "stdio"
      ? { name: name.trim(), description, transport, command: command.trim(), args: args.split("\n").map(a => a.trim()).filter(Boolean), env: table(env) }
      : { name: name.trim(), description, transport, url: url.trim(), headers: table(headers) };
    setBusy(true); setError(null);
    try { onSaved(editing ? await updateMcp(editing.id, input) : await addMcp(input)); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(false); }
  };

  const label = (text: string, field: React.ReactNode) => <label className="mt-3 block text-xs" style={{ color: cream(62) }}>
    <span className="mb-1 block">{text}</span>{field}</label>;

  return <Modal onClose={onClose} width={540}>
    <ModalTitle>{editing ? t("mcp.edit") : t("mcp.add")}</ModalTitle>
    {label(t("mcp.field.name"), <input style={inputStyle()} value={name} onChange={e => setName(e.target.value)} aria-label={t("mcp.field.name")} />)}
    {label(t("mcp.field.description"), <input style={inputStyle()} value={description} onChange={e => setDescription(e.target.value)} aria-label={t("mcp.field.description")} />)}
    <div className="mt-3 flex gap-2" role="radiogroup" aria-label={t("mcp.field.transport")}>
      {(["stdio", "http"] as const).map(kind => <Button key={kind} variant={transport === kind ? "primary" : "ghost"} aria-pressed={transport === kind}
        onClick={() => setTransport(kind)}>{t(`mcp.transport.${kind}`)}</Button>)}
    </div>
    {transport === "stdio" ? <>
      {label(t("mcp.field.command"), <input style={inputStyle()} value={command} onChange={e => setCommand(e.target.value)} aria-label={t("mcp.field.command")} placeholder="npx" />)}
      {label(t("mcp.field.args"), <textarea style={{ ...inputStyle(), minHeight: 64 }} value={args} onChange={e => setArgs(e.target.value)} aria-label={t("mcp.field.args")} />)}
    </> : label(t("mcp.field.url"), <input style={inputStyle()} value={url} onChange={e => setUrl(e.target.value)} aria-label={t("mcp.field.url")} placeholder="https://" />)}
    <div className="mt-3 flex items-center text-xs" style={{ color: cream(62) }}>
      <span className="flex-1">{transport === "stdio" ? t("mcp.field.env") : t("mcp.field.headers")}</span>
      <button type="button" className="skill-tree-icon" title={t("mcp.field.addPair")} aria-label={t("mcp.field.addPair")}
        onClick={() => setPairs([...pairs, { key: "", value: "" }])}><Plus size={14} /></button>
    </div>
    {pairs.map((pair, index) => <div key={index} className="mt-1.5 flex items-center gap-2">
      <input style={{ ...inputStyle(), flex: "0 0 38%" }} value={pair.key} onChange={e => edit(index, { key: e.target.value })}
        aria-label={t("mcp.field.pairName")} placeholder={transport === "stdio" ? "API_KEY" : "Authorization"} />
      <input style={inputStyle()} type="password" autoComplete="off" value={pair.value} onChange={e => edit(index, { value: e.target.value })}
        aria-label={t("mcp.field.pairValue", { name: pair.key || "" })} placeholder={stored.has(pair.key) ? t("mcp.field.keepValue") : ""} />
      <button type="button" className="skill-tree-icon" title={t("mcp.field.removePair")} aria-label={t("mcp.field.removePair")}
        onClick={() => setPairs(pairs.filter((_, i) => i !== index))}><X size={14} /></button>
    </div>)}
    {error && <p role="alert" className="mt-3 text-xs" style={{ color: "var(--danger)" }}>{error}</p>}
    <div className="mt-5 flex justify-end gap-2">
      <Button disabled={busy} onClick={onClose}>{t("ui.cancel")}</Button>
      <Button variant="primary" disabled={busy || !name.trim() || (transport === "stdio" ? !command.trim() : !url.trim())}
        onClick={() => void submit()}>{t("mcp.save")}</Button>
    </div>
  </Modal>;
}
