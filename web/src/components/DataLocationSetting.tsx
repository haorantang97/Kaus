import { useEffect, useState } from "react";
import { HardDrive, LoaderCircle } from "lucide-react";

import { useLocale, type DictKey } from "../i18n";
import { desktopBridge } from "../lib/desktopBridge";

const ERRORS: Record<string, DictKey> = {
  same_location: "settings.dataLocation.error.same",
  nested_location: "settings.dataLocation.error.nested",
  target_not_empty: "settings.dataLocation.error.notEmpty",
  move_failed: "settings.dataLocation.error.failed",
};

/** The home folder as ~, like Finder and Terminal. */
export function shortPath(path: string, home?: string): string {
  return home && (path === home || path.startsWith(`${home}/`)) ? `~${path.slice(home.length)}` : path;
}

/** Settings → General: where the installed app keeps its data (2026-11-24「安装包与后台」③).
 *  Shown only in the installed desktop app; moving is done by the desktop shell. */
export function DataLocationSetting() {
  const { t } = useLocale();
  const [location, setLocation] = useState<{ path: string; home?: string; movable: boolean } | null>(null);
  const [moving, setMoving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void desktopBridge()?.dataLocation?.().then(value => { if (!cancelled) setLocation(value); }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  if (!location?.movable) return null;
  const move = async () => {
    const bridge = desktopBridge();
    if (!bridge?.moveData || moving) return;
    setMoving(true); setError(null);
    try {
      const result = await bridge.moveData();
      if (result.path) setLocation({ ...location, path: result.path });
      else if (result.error) setError(t(ERRORS[result.error] ?? "settings.dataLocation.error.failed"));
    } catch { setError(t("settings.dataLocation.error.failed")); }
    finally { setMoving(false); }
  };
  return <>
    <div className="kaus-settings-preference-row">
      <label id="settings-data-location"><HardDrive size={16} aria-hidden="true" />{t("settings.dataLocation")}</label>
      <span className="kaus-settings-path" title={location.path}>{shortPath(location.path, location.home)}</span>
      <button type="button" className="kaus-inline-action" aria-describedby="settings-data-location" disabled={moving} onClick={() => void move()}>
        {moving ? <LoaderCircle size={14} className="animate-spin" aria-label={t("settings.dataLocation.moving")} /> : t("settings.dataLocation.change")}
      </button>
    </div>
    {error && <p role="alert" className="text-xs" style={{ color: "var(--danger)", margin: 0 }}>{error}</p>}
  </>;
}
