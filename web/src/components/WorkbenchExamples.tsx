import { useEffect, useState } from "react";
import { Download, LoaderCircle } from "lucide-react";

import { useLocale } from "../i18n";
import { fetchWorkbenchExamples, installWorkbenchExample, workbenchFailure, type WorkbenchExample, type WorkbenchModule } from "../lib/workbenchApi";
import { WorkbenchModuleIcon } from "./WorkbenchModuleIcon";

/* 工作台为空时的示例模块：随 Kaus 分发，一键安装。权限仍要用户在模块配置里给。 */
export function WorkbenchExamples({ onInstalled }: { onInstalled: (module: WorkbenchModule) => void }) {
  const { t } = useLocale();
  const [examples, setExamples] = useState<WorkbenchExample[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    fetchWorkbenchExamples(controller.signal)
      .then(result => setExamples(Array.isArray(result?.examples) ? result.examples.filter(item => !item.installed) : []))
      .catch(() => setExamples([]));
    return () => controller.abort();
  }, []);
  if (!examples.length) return null;
  const install = async (id: string) => {
    setBusy(id); setError(null);
    try { onInstalled(await installWorkbenchExample(id)); }
    catch (failure) { setError(workbenchFailure(failure)); }
    finally { setBusy(null); }
  };
  return (
    <section className="workbench-examples" aria-label={t("workbench.examples.title")}>
      <h2>{t("workbench.examples.title")}</h2>
      {error && <div className="workbench-inline-error" role="alert">{error}</div>}
      {examples.map(item => (
        <div key={item.id} className="workbench-example">
          <div className="workbench-module-icon"><WorkbenchModuleIcon module={{ id: item.id, icon: item.icon, name: item.name } as WorkbenchModule} size={20} /></div>
          <div className="workbench-example-text"><strong>{item.name}</strong>{item.description && <span>{item.description}</span>}</div>
          <button className="workbench-subtle" disabled={busy !== null} aria-label={`${t("workbench.examples.install")} ${item.name}`} onClick={() => void install(item.id)}>
            {busy === item.id ? <LoaderCircle size={16} className="kaus-ui-spin" /> : <Download size={16} />}{t("workbench.examples.install")}
          </button>
        </div>
      ))}
    </section>
  );
}
