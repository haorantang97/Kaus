import { WorkbenchModuleIcon } from "../components/WorkbenchModuleIcon";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowUpRight, LoaderCircle, MoreHorizontal, Pin, PinOff, Plus, Power, RefreshCw, Settings2, Trash2, X } from "lucide-react";
import { useLocale } from "../i18n";
import { confirmAsync } from "../components/ui";
import { WorkspacePicker } from "../components/WorkspacePicker";
import { WorkbenchConfig } from "../components/WorkbenchConfig";
import { WorkbenchIntake } from "../components/WorkbenchIntake";
import { ModuleFrame } from "../components/ModuleFrame";
import { writeModuleDrag } from "../components/WorkbenchNav";
import { chooseNativeWorkspace } from "../lib/workspaceApi";
import { activeWorkbenchIntake, dismissWorkbenchIntake, fetchWorkbenchIntake, fetchWorkbenchIntakes, inspectWorkbenchProject, notifyWorkbenchChanged, patchWorkbenchModule, removeWorkbenchModule, workbenchFailure, type WorkbenchModule, type WorkbenchIntake as Intake } from "../lib/workbenchApi";
import { workbenchPath } from "../lib/routes";
import "../components/workbench.css";
export function WorkbenchPage({moduleId,modules,loading,failed,onRefresh,navigate,onConversation,intakeId=null,onIntakeConversation}: {moduleId:string|null; modules:WorkbenchModule[];loading:boolean;failed:boolean;onRefresh:()=>void;navigate:(path:string)=>void;onConversation:(id:string,moduleId:string)=>void;intakeId?:string|null;onIntakeConversation?:(id:string,intakeId:string)=>void}) {
  const {t,locale} = useLocale();
  const [config,setConfig]=useState<string|null>(null),[menu,setMenu]=useState<string|null>(null),[picker,setPicker]=useState(false),[busy,setBusy]=useState(false),[actionError,setActionError]=useState<string|null>(null);
  const [intakes,setIntakes]=useState<Intake[]>([]), [selectedIntake,setSelectedIntake]=useState<Intake|null>(null), [installed,setInstalled]=useState<WorkbenchModule|null>(null);
  const dismissedIntakes=useRef(new Set<string>());
  useEffect(()=> {
    if(moduleId)return;
    const controller=new AbortController(); let timer:ReturnType<typeof setTimeout>;
    async function load(){try{const data=await fetchWorkbenchIntakes(controller.signal);if(controller.signal.aborted||!Array.isArray(data.intakes))return;setIntakes(data.intakes.filter(item=>!dismissedIntakes.current.has(item.id)));if(data.intakes.some(activeWorkbenchIntake))timer=setTimeout(()=>void load(),3000);}catch{/* A background list failure does not interrupt the library. */}}
    void load();return()=>{controller.abort();clearTimeout(timer);};
  },[moduleId,selectedIntake?.id]);
  useEffect(()=>{
    if(!intakeId)return;
    const controller=new AbortController();
    void fetchWorkbenchIntake(intakeId,controller.signal).then(item=>{if(!controller.signal.aborted){if(item.status==="installed"&&item.moduleId)navigate(workbenchPath(item.moduleId));else setSelectedIntake(item);}}).catch(()=>{if(!controller.signal.aborted)setActionError(t("workbench.loadFailed"));});
    return()=>controller.abort();
  },[intakeId,navigate,t]);
  useEffect(()=> {
    if (!menu) return;
    const outside=(event:PointerEvent)=>{if(!(event.target as Element)?.closest(".workbench-menu"))setMenu(null);};
    const escape=(event:KeyboardEvent)=>{if(event.key==="Escape")setMenu(null);};
    document.addEventListener("pointerdown",outside);document.addEventListener("keydown",escape);
    return ()=>{document.removeEventListener("pointerdown",outside);document.removeEventListener("keydown",escape);};
  },[menu]);
  const module=modules.find(item=>item.id===moduleId)??(installed?.id===moduleId?installed:undefined), configure=modules.find(item=>item.id===config)??(installed?.id===config?installed:undefined);
  async function mutate(action:()=>Promise<unknown>) {setBusy(true);setActionError(null);try{await action();notifyWorkbenchChanged();}catch(error){setActionError(workbenchFailure(error,locale==="en"));}finally{setBusy(false);}}
  function updateIntake(item:Intake){dismissedIntakes.current.delete(item.id);setIntakes(previous=>[item,...previous.filter(value=>value.id!==item.id)]);}
  async function dismiss(item:Intake) {await mutate(async()=>{await dismissWorkbenchIntake(item.id);dismissedIntakes.current.add(item.id);setIntakes(previous=>previous.filter(value=>value.id!==item.id));});}
  async function inspect(path:string) {setBusy(true);setActionError(null);setPicker(false);try{const result=await inspectWorkbenchProject(path);updateIntake(result);setSelectedIntake(result);}catch{setActionError(t("workbench.intake.inspectFailed"));}finally{setBusy(false);}}
  async function pick() {setBusy(true);setActionError(null);try{const path=await chooseNativeWorkspace(null);if(path===undefined)setPicker(true);else if(path)await inspect(path);}catch{setActionError(t("workbench.intake.inspectFailed"));}finally{setBusy(false);}}
  function closeIntake(){setSelectedIntake(null);if(intakeId)navigate(workbenchPath());}
  async function remove(item:WorkbenchModule) {setMenu(null);if(!await confirmAsync(t("workbench.removeConfirm"),{okLabel:t("workbench.remove"),danger:true}))return;await mutate(()=>removeWorkbenchModule(item.id));}
  const actions=(item:WorkbenchModule)=><div className="workbench-menu" role="menu" onKeyDown={event=>{if(event.key==="Escape")setMenu(null);}}>
    <button role="menuitem" onClick={()=>{setMenu(null);setConfig(item.id);}}><Settings2 size={15}/>{t("workbench.config")}</button>
    <button role="menuitem" disabled={busy||!item.enabled} onClick={()=>{setMenu(null);void mutate(()=>patchWorkbenchModule(item.id,{pinned:!item.pinned}));}}>{item.pinned?<PinOff size={15}/>:<Pin size={15}/>} {t(item.pinned?"workbench.unpin":"workbench.pin")}</button>
    <button role="menuitem" disabled={busy} onClick={()=>{setMenu(null);void mutate(()=>patchWorkbenchModule(item.id,{enabled:!item.enabled}));}}><Power size={15}/>{t(item.enabled?"workbench.disable":"workbench.enable")}</button>
    {!item.builtin&&<button role="menuitem" disabled={busy} onClick={()=>void remove(item)}><Trash2 size={15}/>{t("workbench.remove")}</button>}
  </div>;
  return <div className={`workbench-page ${moduleId?"is-module":""}`}>
    <header className="workbench-header"><div className="workbench-heading">{moduleId&&<button className="kaus-chat-icon-action" aria-label={t("workbench.title")} title={t("workbench.title")} onClick={()=>navigate(workbenchPath())}><ArrowLeft size={18}/></button>}<h1>{module?.name??t("workbench.title")}</h1></div><div className="workbench-actions">
      {module?<><button className={`kaus-chat-icon-action ${module.pinned?"is-active":""}`} aria-label={t(module.pinned?"workbench.unpin":"workbench.pin")} title={t(module.pinned?"workbench.unpin":"workbench.pin")} disabled={busy||!module.enabled} onClick={()=>void mutate(()=>patchWorkbenchModule(module.id,{pinned:!module.pinned}))}>{module.pinned?<PinOff size={17}/>:<Pin size={17}/>}</button><button className="kaus-chat-icon-action" title={t("workbench.config")} aria-label={t("workbench.config")} onClick={()=>setConfig(module.id)}><Settings2 size={18}/></button></>:<button className="workbench-primary" disabled={busy} onClick={()=>void pick()}>{busy?<LoaderCircle size={17} className="kaus-ui-spin"/>:<Plus size={17}/>} {t("workbench.add")}</button>}
    </div></header>
    {actionError&&<div className="workbench-inline-error" role="alert">{actionError}</div>}
    {!moduleId&&intakes.some(item=>item.status!=="installed")&&<div className="workbench-intake-list" aria-label={t("workbench.intake.tasks")}>{intakes.filter(item=>item.status!=="installed").map(item=><div key={item.id} className="workbench-intake-row"><button className="workbench-intake-open" onClick={()=>setSelectedIntake(item)}>{activeWorkbenchIntake(item)?<LoaderCircle size={15} className="kaus-ui-spin"/>:<ArrowUpRight size={15}/>}<strong>{item.moduleName||item.displayName}</strong><span>{t((`workbench.intake.status.${item.status}`) as import("../i18n").DictKey)}</span></button>{!activeWorkbenchIntake(item)&&<button className="kaus-chat-icon-action" disabled={busy} aria-label={`${item.moduleName||item.displayName} · ${t("workbench.intake.dismiss")}`} title={t("workbench.intake.dismiss")} onClick={()=>void dismiss(item)}><X size={15}/></button>}</div>)}</div>}
    {loading&&!modules.length?<div className="workbench-empty"><LoaderCircle size={22} className="kaus-ui-spin" aria-label={t("ui.loading")}/></div>:failed&&!modules.length?<div className="workbench-empty"><span>{t("workbench.unavailable")}</span><button className="workbench-subtle" onClick={onRefresh}><RefreshCw size={16}/>{t("workbench.retry")}</button></div>:moduleId?module?module.enabled?module.permissions.length&&!module.grants.length?<div className="workbench-empty"><button className="workbench-primary" onClick={()=>setConfig(module.id)}><Settings2 size={17}/>{t("workbench.config")}</button></div>:<ModuleFrame key={`frame:${module.id}`} module={module} onConfigure={()=>setConfig(module.id)} onConversation={id=>onConversation(id,module.id)}/>:<div className="workbench-empty"><span>{t("workbench.disabled")}</span><button className="workbench-primary" disabled={busy} onClick={()=>void mutate(()=>patchWorkbenchModule(module.id,{enabled:true}))}><Power size={16}/>{t("workbench.enable")}</button></div>:<div className="workbench-empty">{t("workbench.noModule")}</div>:<div className="workbench-library">
      {!modules.length&&<div className="workbench-empty">{t("workbench.empty")}</div>}
      {modules.map(item=><article key={item.id} className={`workbench-card ${!item.enabled?"is-disabled":""}`} draggable={item.enabled} onDragStart={event=>writeModuleDrag(event,item.id)}>
        <div className="workbench-card-top"><div className="workbench-module-icon"><WorkbenchModuleIcon module={item} size={25}/></div><button className="kaus-chat-icon-action" aria-label={`${item.name} · ${t("common.more")}`} onClick={()=>setMenu(menu===item.id?null:item.id)}><MoreHorizontal size={18}/></button>{menu===item.id&&actions(item)}</div>
        <button className="workbench-card-open" onClick={()=>navigate(workbenchPath(item.id))}><h2>{item.name}</h2>{item.description&&<p>{item.description}</p>}<footer><span>{t(!item.enabled?"workbench.disabled":"workbench.external")}</span><ArrowUpRight size={18}/></footer></button>
      </article>)}
    </div>}
    {picker&&<WorkspacePicker initialPath={null} onSelect={inspect} onClose={()=>setPicker(false)}/>}
    {selectedIntake&&<WorkbenchIntake key={`intake:${selectedIntake.id}`} initial={selectedIntake} onClose={closeIntake} onUpdate={updateIntake} onInstalled={item=>{setInstalled(item);setSelectedIntake(null);navigate(workbenchPath(item.id));}} onConversation={(id,source)=>{setSelectedIntake(null);onIntakeConversation?.(id,source);}}/>}
    {configure&&<WorkbenchConfig key={`config:${configure.id}`} module={configure} onClose={()=>setConfig(null)}/>}
  </div>;
}
