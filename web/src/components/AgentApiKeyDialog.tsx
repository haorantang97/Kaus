import { useState } from "react";

import { useLocale } from "../i18n";
import { clearAgentApiKey, describeFailure, saveAgentApiKey } from "../lib/sessionApi";
import { Button, Modal, ModalTitle, inputStyle, toast } from "./ui";

/* 给一台 Agent 填 API key：保存后 Kaus 启动它时不走订阅登录，按 API 计费；清除后回到订阅。
   key 只写不读：这里永远看不到已保存的值。 */
export function AgentApiKeyDialog({ backendId, name, envName, configured, onClose }: {
  backendId: string;
  name: string;
  envName: string;
  configured: boolean;
  onClose: (changed: boolean) => void;
}) {
  const { t } = useLocale();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const act = async (work: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try { await work(); toast(done, "ok"); onClose(true); }
    catch (failure) { toast(describeFailure(failure), "bad"); }
    finally { setBusy(false); }
  };
  return (
    <Modal onClose={() => onClose(false)} width={440}>
      <ModalTitle>{t("engines.apiKey.title", { name })}</ModalTitle>
      <p className="mb-3 text-xs leading-relaxed" style={{ color: "color-mix(in srgb, var(--midground-base) 60%, transparent)" }}>
        {t("engines.apiKey.note", { env: envName })}
      </p>
      <input type="password" autoComplete="off" spellCheck={false} value={value} onChange={e => setValue(e.target.value)}
        placeholder={configured ? t("engines.apiKey.replace") : t("engines.apiKey.placeholder")} aria-label={t("engines.apiKey.field")}
        style={inputStyle()} onKeyDown={e => { if (e.key === "Enter" && value.trim()) void act(() => saveAgentApiKey(backendId, value), t("engines.apiKey.saved")); }} />
      <div className="mt-4 flex justify-between gap-2">
        {configured ? <Button disabled={busy} onClick={() => void act(() => clearAgentApiKey(backendId), t("engines.apiKey.cleared"))}>{t("engines.apiKey.clear")}</Button> : <span />}
        <div className="flex gap-2">
          <Button disabled={busy} onClick={() => onClose(false)}>{t("common.cancel")}</Button>
          <Button variant="primary" disabled={busy || !value.trim()} onClick={() => void act(() => saveAgentApiKey(backendId, value), t("engines.apiKey.saved"))}>{t("common.save")}</Button>
        </div>
      </div>
    </Modal>
  );
}
