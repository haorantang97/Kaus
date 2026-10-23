import { useEffect, useState } from "react";
import { Download, LoaderCircle, RotateCcw } from "lucide-react";
import { useLocale } from "../i18n";
import { runtimeVersion, type RuntimeVersion } from "../lib/runtimeManagement";
import { ErrorNotice } from "./ui";
import { describeFailure } from "../lib/sessionApi";

export function RuntimeVersions({ presetId }: { presetId: string }) {
  const { t } = useLocale();
  const [data, setData] = useState<RuntimeVersion | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = data?.job?.state === "installing" || data?.job?.state === "verifying";
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const read = () => { void runtimeVersion(presetId, undefined, undefined, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      setData(value);
      if (value.job?.state === "installing" || value.job?.state === "verifying") timer = setTimeout(read, 1500);
    }).catch(failure => { if (!controller.signal.aborted) setError(describeFailure(failure)); }); };
    read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [presetId, pending]);
  const change = async (action: "install" | "rollback") => {
    if (!data) return;
    setBusy(true); setError("");
    try { setData(await runtimeVersion(presetId, action, data.revision)); }
    catch (failure) { setError(describeFailure(failure)); }
    finally { setBusy(false); }
  };
  if (!data && !error) return null;
  return <div className="mt-3 space-y-2 text-xs" data-testid="runtime-versions">
    {data?.supported && <div className="flex items-center justify-between gap-3">
      <span>{t("engines.runtime.managed")} <span className="opacity-60">{data.version || data.recommendedVersion}</span></span>
      <span className="inline-flex items-center gap-2">
        {pending ? <LoaderCircle size={14} className="animate-spin" aria-label={t("engines.runtime.installing")} /> : <button type="button" className="kaus-chat-icon-action" disabled={busy || (data.available && data.version === data.recommendedVersion)}
          aria-label={t("engines.runtime.install")} title={t("engines.runtime.install")} onClick={() => void change("install")}><Download size={15} /></button>}
        {data.previousVersion && <button type="button" className="kaus-chat-icon-action" disabled={busy || pending}
          aria-label={t("engines.runtime.rollback")} title={`${t("engines.runtime.rollback")} · ${data.previousVersion}`} onClick={() => void change("rollback")}><RotateCcw size={15} /></button>}
      </span>
    </div>}
    {data?.skillBuild && <div className="flex items-center justify-between gap-3">
      <span>{t("engines.runtime.skillBuild")}</span>
      <span className="opacity-60">{data.skillBuild.available ? `${data.skillBuild.sourceVersion} · ${data.skillBuild.patchVersion}` : t("engines.runtime.skillBuildInvalid")}</span>
    </div>}
    {(error || data?.job?.error) && <ErrorNotice error={error || data?.job?.error} />}
    {data?.job?.refreshPending && <p role="status">{t("engines.connect.pendingUpdate")}</p>}
  </div>;
}
