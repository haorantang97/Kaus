import { useEffect, useId, useState } from "react";
import { Check, LoaderCircle, ShieldCheck, Terminal } from "lucide-react";
import { useLocale } from "../i18n";
import { describeFailure } from "../lib/sessionApi";
import { fetchTerminalSettings, saveTerminalSettings, type TerminalSettingsWire } from "../lib/terminalApi";
import { cream } from "./ui";
import { SelectPopover } from "./SelectPopover";

export function TerminalSettings() {
  const { t } = useLocale();
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
  return <section className="flex flex-col gap-2" aria-label={t("terminal.settings.section")}>
    <div className="kaus-settings-preference-row">
      <label htmlFor={id}><Terminal size={16} aria-hidden="true" />{t("terminal.settings.default")}</label>
      <SelectPopover id={id} label={t("terminal.settings.default")} value={settings?.app ?? ""} disabled={!settings || saving}
        placeholder={t("terminal.settings.loading")} searchable={false} onChange={app => void save({ app })}
        options={[
          ...(settings && !settings.launchers.some(item => item.id === settings.app) ? [{ value: settings.app, label: settings.app, disabled: true }] : []),
          ...(settings?.launchers.map(item => ({ value: item.id, label: item.label, disabled: !item.installed,
            description: item.installed ? undefined : t("terminal.settings.notInstalled") })) ?? []),
        ]} />
      <span className="kaus-settings-save-status" role="status" aria-label={saving ? t("terminal.settings.saving") : saved ? t("terminal.settings.saved") : undefined}>
        {saving ? <LoaderCircle className="size-4 animate-spin" aria-hidden="true" /> : saved ? <Check className="size-4" style={{ color: "var(--accent-text)" }} aria-hidden="true" /> : null}
      </span>
    </div>
    <div className="kaus-settings-preference-row">
      <label id={`${id}-trust`}><ShieldCheck size={16} aria-hidden="true" />{t("terminal.settings.trust")}</label>
      <button type="button" role="switch" className="kaus-switch" aria-labelledby={`${id}-trust`} aria-checked={settings?.trustWorkspace ?? false}
        disabled={!settings || saving} title={t("terminal.settings.trustHint")}
        onClick={() => settings && void save({ trustWorkspace: !settings.trustWorkspace })}><span /></button>
    </div>
    {error && <p role="alert" className="text-xs" style={{ color: "var(--danger)", margin: 0 }}>{error}</p>}
    {!settings && !error && <span className="sr-only" style={{ color: cream(50) }}>{t("terminal.settings.reading")}</span>}
  </section>;
}
