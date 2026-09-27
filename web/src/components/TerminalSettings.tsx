import { useEffect, useId, useState } from "react";
import { Check, LoaderCircle, Terminal } from "lucide-react";
import { useLocale } from "../i18n";
import { describeFailure } from "../lib/sessionApi";
import { fetchTerminalSettings, saveTerminalSettings, type TerminalSettingsWire } from "../lib/terminalApi";
import { cream, inputStyle } from "./ui";

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
  async function select(app: string) {
    setSaving(true); setError(null); setSaved(false);
    try { setSettings(await saveTerminalSettings(app)); setSaved(true); }
    catch (failure) { setError(describeFailure(failure)); }
    finally { setSaving(false); }
  }
  return <section className="flex flex-col gap-2" aria-label={en ? "Terminal" : "终端"}>
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <Terminal className="size-4 shrink-0" aria-hidden="true" />
      <label htmlFor={id} className="flex-1">{en ? "Default terminal" : "默认终端"}</label>
      <select id={id} value={settings?.app ?? ""} disabled={!settings || saving}
        style={{ ...inputStyle(), width: 180 }} onChange={(event) => void select(event.target.value)}>
        {!settings && <option value="">{en ? "Loading…" : "加载中…"}</option>}
        {settings && !settings.launchers.some((item) => item.id === settings.app) && <option value={settings.app} disabled>{settings.app}</option>}
        {settings?.launchers.map((item) => <option key={item.id} value={item.id} disabled={!item.installed}>
          {item.label}{item.installed ? "" : en ? " · Not installed" : " · 未安装"}
        </option>)}
      </select>
      <span className="size-4" role="status" aria-label={saving ? (en ? "Saving" : "保存中") : saved ? (en ? "Saved" : "已保存") : undefined}>
        {saving ? <LoaderCircle className="size-4 animate-spin" aria-hidden="true" /> : saved ? <Check className="size-4" style={{ color: "var(--accent-text)" }} aria-hidden="true" /> : null}
      </span>
    </div>
    {error && <p role="alert" className="text-xs" style={{ color: "var(--danger)", margin: 0 }}>{error}</p>}
    {!settings && !error && <span className="sr-only" style={{ color: cream(50) }}>{en ? "Loading terminal settings" : "正在读取终端设置"}</span>}
  </section>;
}
