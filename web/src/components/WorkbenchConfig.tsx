import { useState } from "react";
import { Check, LoaderCircle, X } from "lucide-react";
import { useLocale, type DictKey } from "../i18n";
import { Modal } from "./ui";
import { WorkbenchRuntimeFields } from "./WorkbenchRuntimeFields";
import { useWorkbenchRuntime } from "../hooks/useWorkbenchRuntime";
import { patchWorkbenchModule, notifyWorkbenchChanged, workbenchFailure, type WorkbenchModule } from "../lib/workbenchApi";

export function WorkbenchConfig({module, onClose}: {module: WorkbenchModule; onClose: () => void}) {
  const {t,locale} = useLocale();
  const runtime = useWorkbenchRuntime(module);
  const [grants,setGrants] = useState(module.grants);
  const [busy,setBusy] = useState(false), [saveError,setSaveError] = useState<string|null>(null);
  async function save() {
    setBusy(true); setSaveError(null);
    try { await patchWorkbenchModule(module.id,{...runtime.value,grants}); notifyWorkbenchChanged(); onClose(); }
    catch (error) { setSaveError(workbenchFailure(error,locale==="en")); } finally {setBusy(false);}
  }
  return <Modal onClose={onClose} width={520}><section className="workbench-config" role="dialog" aria-modal="true" aria-label={t("workbench.config")}>
    <header><h2>{module.name}</h2><button className="kaus-chat-icon-action" aria-label={t("common.close")} onClick={onClose}><X size={18}/></button></header>
    <WorkbenchRuntimeFields runtime={runtime} disabled={busy}/>
    {!!module.permissions.length && <fieldset><legend>{t("workbench.access")}</legend>{module.permissions.map(permission=><label className="workbench-grant" key={permission}><input type="checkbox" disabled={busy} checked={grants.includes(permission)} onChange={event=>setGrants(previous=>event.target.checked?[...previous,permission]:previous.filter(value=>value!==permission))}/><span>{t((`workbench.grant.${permission}`) as DictKey)}</span></label>)}</fieldset>}
    {saveError&&<p role="alert" className="workbench-inline-error">{saveError}</p>}
    <footer><button className="workbench-primary" disabled={busy||runtime.loading||runtime.busy||(grants.includes("agent:run")&&!runtime.ready)} onClick={()=>void save()}>{busy?<LoaderCircle size={16} className="kaus-ui-spin"/>:<Check size={16}/>} {t("workbench.save")}</button></footer>
  </section></Modal>;
}
