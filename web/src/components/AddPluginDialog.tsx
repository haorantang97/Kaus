import { useState } from "react";
import { FolderOpen, LoaderCircle } from "lucide-react";

import { useLocale } from "../i18n";
import { inspectPluginSource, installPlugin, type InspectResult } from "../lib/libraryApi";
import { describeFailure } from "../lib/sessionApi";
import { chooseNativeWorkspace } from "../lib/workspaceApi";
import { Button, Modal, ModalTitle, Pill, inputStyle } from "./ui";
import { WorkspacePicker } from "./WorkspacePicker";

/* 添加插件：本地文件夹或 Git 地址 → 检查 → 安装。插件市场仓库先列出其中的插件，选一个再装。
   装好后出现在能力库（技能树）里，拖到项目上启用。 */
export function AddPluginDialog({ onClose, onInstalled }: { onClose: () => void; onInstalled: () => void }) {
  const { t } = useLocale();
  const [source, setSource] = useState("");
  const [result, setResult] = useState<InspectResult | null>(null);
  const [choice, setChoice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  const inspect = async (value = source) => {
    if (!value.trim()) return;
    setBusy(true); setError(null); setResult(null); setChoice(null);
    try { setResult(await inspectPluginSource(value.trim())); }
    catch (failure) { setError(describeFailure(failure)); }
    finally { setBusy(false); }
  };
  const pickFolder = async () => {
    const native = await chooseNativeWorkspace(null);
    if (native === undefined) { setPicking(true); return; }
    if (native) { setSource(native); void inspect(native); }
  };
  const install = async () => {
    setBusy(true); setError(null);
    try { await installPlugin(source.trim(), result?.kind === "marketplace" ? choice ?? undefined : undefined); onInstalled(); }
    catch (failure) { setError(describeFailure(failure)); }
    finally { setBusy(false); }
  };
  const preview = result?.kind === "plugin" ? result.plugin : null;
  const contents = preview?.contents;
  const canInstall = !busy && (preview !== null || (result?.kind === "marketplace" && choice !== null));

  return (
    <Modal onClose={onClose} width={480}>
      <ModalTitle>{t("library.add.title")}</ModalTitle>
      <div className="flex gap-2">
        <input value={source} onChange={e => { setSource(e.target.value); setResult(null); }} placeholder={t("library.add.placeholder")}
          aria-label={t("library.add.source")} style={inputStyle()} onKeyDown={e => { if (e.key === "Enter") void inspect(); }} />
        <Button aria-label={t("library.add.folder")} title={t("library.add.folder")} disabled={busy} onClick={() => void pickFolder()}><FolderOpen size={15} /></Button>
        <Button disabled={busy || !source.trim()} onClick={() => void inspect()}>{busy && !result ? <LoaderCircle size={15} className="kaus-ui-spin" /> : t("library.add.inspect")}</Button>
      </div>
      {error && <p role="alert" className="mt-3 text-xs" style={{ color: "var(--danger)" }}>{error}</p>}
      {preview && contents && (
        <div className="kaus-plugin-preview" data-testid="plugin-preview">
          <div className="flex items-center gap-2"><strong>{preview.name}</strong>{preview.version && <span>{preview.version}</span>}<Pill tone="muted">{t(`library.format.${preview.format}`)}</Pill></div>
          {preview.description && <p>{preview.description}</p>}
          <p>{[contents.skills ? t("library.contents.skills", { count: contents.skills }) : null,
            contents.mcpServers.length ? t("library.contents.mcp", { count: contents.mcpServers.length }) : null,
            contents.hooks ? t("library.contents.hooks") : null, contents.commands ? t("library.contents.commands") : null,
            contents.agents ? t("library.contents.agents") : null].filter(Boolean).join(" · ") || t("library.contents.empty")}</p>
        </div>
      )}
      {result?.kind === "marketplace" && (
        <div className="kaus-plugin-preview" role="radiogroup" aria-label={t("library.marketplace", { name: result.name })}>
          <strong>{t("library.marketplace", { name: result.name })}</strong>
          {result.plugins.map(item => (
            <label key={item.name} className="kaus-plugin-choice">
              <input type="radio" name="plugin" checked={choice === item.name} onChange={() => setChoice(item.name)} />
              <span><strong>{item.name}</strong>{item.description && <small>{item.description}</small>}</span>
            </label>
          ))}
        </div>
      )}
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose} disabled={busy}>{t("common.cancel")}</Button>
        <Button variant="primary" disabled={!canInstall} onClick={() => void install()}>{t("library.add.install")}</Button>
      </div>
      {picking && <WorkspacePicker initialPath={null} title={t("library.add.folder")} onClose={() => setPicking(false)}
        onSelect={async path => { setPicking(false); setSource(path); await inspect(path); }} />}
    </Modal>
  );
}
