import { useEffect, useId, useState } from "react";
import { Check, LoaderCircle, ShieldCheck, Terminal } from "lucide-react";
import { useLocale } from "../i18n";
import { describeFailure } from "../lib/sessionApi";
import { fetchTerminalSettings, saveTerminalSettings, type TerminalSettingsWire } from "../lib/terminalApi";
import { cream } from "./ui";
import { SelectPopover } from "./SelectPopover";

export function TerminalSettings() {
  const { locale } = useLocale();
  const en = locale === "en";
  const id = useId();
  const [settings, setSettings] = useState<TerminalSettingsWire | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let cancelled = false;
    fetchTerminalSettings().then((value) => { if (!cancelled) setSettings(value); })
      .catch((failure) => { if (!cancelled) setError(describeFailure(failure)); });
    return () => { cancelled = true; };
  }, []);
  async function save(body: { app?: string; trustWorkspace?: boolean }) {
    setSaving(true); setError(null); setSaved(false);
    try { setSettings(await saveTerminalSettings(body)); setSaved(true); }
    catch (failure) { setError(describeFailure(failure)); }
    finally { setSaving(false); }
  }
  return <section className="flex flex-col gap-2" aria-label={en ? "Terminal" : "终端"}>
    <div className="kaus-settings-preference-row">
      <label htmlFor={id}><Terminal size={16} aria-hidden="true" />{en ? "Default terminal" : "默认终端"}</label>
      <SelectPopover id={id} label={en ? "Default terminal" : "默认终端"} value={settings?.app ?? ""} disabled={!settings || saving}
        placeholder={en ? "Loading…" : "加载中…"} searchable={false} onChange={app => void save({ app })}
        options={[
          ...(settings && !settings.launchers.some(item => item.id === settings.app) ? [{ value: settings.app, label: settings.app, disabled: true }] : []),
          ...(settings?.launchers.map(item => ({ value: item.id, label: item.label, disabled: !item.installed,
            description: item.installed ? undefined : en ? "Not installed" : "未安装" })) ?? []),
        ]} />
      <span className="kaus-settings-save-status" role="status" aria-label={saving ? (en ? "Saving" : "保存中") : saved ? (en ? "Saved" : "已保存") : undefined}>
        {saving ? <LoaderCircle className="size-4 animate-spin" aria-hidden="true" /> : saved ? <Check className="size-4" style={{ color: "var(--accent-text)" }} aria-hidden="true" /> : null}
      </span>
    </div>
    <div className="kaus-settings-preference-row">
      <label id={`${id}-trust`}><ShieldCheck size={16} aria-hidden="true" />{en ? "Trust project folder" : "自动信任项目目录"}</label>
      <button type="button" role="switch" className="kaus-switch" aria-labelledby={`${id}-trust`} aria-checked={settings?.trustWorkspace ?? false}
        disabled={!settings || saving} title={en ? "Skip the agent's trust prompt when opening a terminal" : "打开终端时跳过 Agent 的目录信任询问"}
        onClick={() => settings && void save({ trustWorkspace: !settings.trustWorkspace })}><span /></button>
    </div>
    {error && <p role="alert" className="text-xs" style={{ color: "var(--danger)", margin: 0 }}>{error}</p>}
    {!settings && !error && <span className="sr-only" style={{ color: cream(50) }}>{en ? "Loading terminal settings" : "正在读取终端设置"}</span>}
  </section>;
}
