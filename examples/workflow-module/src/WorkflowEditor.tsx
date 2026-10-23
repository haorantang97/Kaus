import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Check, ChevronRight, GripVertical, LoaderCircle, Play, Plus, Save, Square, Trash2 } from "lucide-react";
import { useLocale, type DictKey } from "./i18n";
import { defaultWorkflow, activeModuleJob, cancelModuleJob, fetchModuleJobs, fetchWorkflow, resumeModuleJob, runWorkflow, saveWorkflow, workbenchFailure, type ModuleJob, type WorkbenchModule, type WorkflowDefinition } from "./hostApi";
const key = () => crypto.randomUUID();
export function WorkflowEditor({module,onConfigure,onConversation}: {module:WorkbenchModule;onConfigure:()=>void;onConversation:(id:string)=>void}) {
  const {t,locale} = useLocale();
  const [definition,setDefinition] = useState<WorkflowDefinition|null>(null), [input,setInput] = useState("");
  const [jobs,setJobs] = useState<ModuleJob[]>([]), [selected,setSelected] = useState<string|null>(null);
  const [busy,setBusy] = useState(false), [dirty,setDirty] = useState(false), [failed,setFailed] = useState<string|null>(null);
  const [drag,setDrag] = useState<string|null>(null);
  const mounted = useRef(true), mutation = useRef(false), loadRevision = useRef(0);
  const runKey = useRef<string|null>(null);
  const job = jobs.find(item=>item.id===selected)??jobs[0];
  const active = jobs.some(activeModuleJob);
  const configured = Boolean(module.projectId&&module.bindingId&&module.grants.includes("agent:run"));
  const reload = useCallback(async (signal?:AbortSignal) => {
    const current=++loadRevision.current;
    const data=await fetchModuleJobs(module.id,signal);
    if (!signal?.aborted&&mounted.current&&current===loadRevision.current)setJobs(data.jobs);
  },[module.id]);
  useEffect(()=> {
    mounted.current=true;
    const controller=new AbortController();
    void Promise.all([fetchWorkflow(module.id).then(value=> {if(!controller.signal.aborted)setDefinition(value??structuredClone(defaultWorkflow));}),reload(controller.signal)]).catch(()=> {if(!controller.signal.aborted)setFailed(t("workbench.loadFailed"));});
    const timer=window.setInterval(()=>void reload(controller.signal).catch(()=>{}),2000);
    return ()=>{mounted.current=false;controller.abort();window.clearInterval(timer);};
  },[module.id,reload]);
  function edit(next: WorkflowDefinition) {setDefinition(next);setDirty(true);runKey.current=null;}
  function move(id:string,target:number) {if(!definition)return;const steps=[...definition.steps],index=steps.findIndex(step=>step.id===id);if(index<0||target<0||target>=steps.length)return;const [step]=steps.splice(index,1);steps.splice(target,0,step);edit({...definition,steps});}
  async function mutate(action:()=>Promise<unknown>) {
    if(mutation.current)return;mutation.current=true;setBusy(true);setFailed(null);
    try {await action();await reload();}catch (error) {if(mounted.current)setFailed(workbenchFailure(error,locale==="en"));}finally{mutation.current=false;if(mounted.current)setBusy(false);}
  }
  async function start() {
    if(!definition||!configured||active)return;
    await mutate(async()=> {
      await saveWorkflow(module.id,definition);setDirty(false);
      const idempotencyKey=runKey.current??(runKey.current=key());
      const next=await runWorkflow(module.id,input,idempotencyKey);
      setSelected(next.id);runKey.current=null;
    });
  }
  const valid = Boolean(definition?.name.trim()&&definition?.steps.length&&definition.steps.every(step=>step.title.trim()&&step.prompt.trim()));
  return <div className="workbench-workflow">
    <section className="workflow-editor">
      <header><input aria-label={t("workbench.workflowName")} maxLength={100} value={definition?.name??""} disabled={!definition||busy||active} onChange={event=>definition&&edit({...definition,name:event.target.value})}/><button className="kaus-chat-icon-action" aria-label={t("workbench.save")} title={t("workbench.save")} disabled={!dirty||busy||active||!valid} onClick={()=>void mutate(async()=>{if(definition){await saveWorkflow(module.id,definition);setDirty(false);}})}><Save size={17}/></button></header>
      <div className="workflow-steps">
      {definition?.steps.map((step,index)=><div key={step.id} className="workflow-step" draggable={!busy&&!active} onDragStart={event=>{setDrag(step.id);event.dataTransfer.effectAllowed="move";}} onDragOver={event=>{if(drag)event.preventDefault();}} onDrop={event=>{event.preventDefault();if(drag)move(drag,index);setDrag(null);}} onDragEnd={()=>setDrag(null)}>
        <header><GripVertical size={15} className="workflow-grip"/><span className="workflow-step-number">{index+1}</span><input aria-label={`${t("workbench.stepName")} ${index+1}`} maxLength={100} value={step.title} disabled={busy||active} onChange={event=>edit({...definition,steps:definition.steps.map(value=>value.id===step.id?{...value,title:event.target.value}:value)})}/>
          <button className="kaus-chat-icon-action" aria-label={`${t("workbench.up")} ${index+1}`} disabled={busy||active||index===0} onClick={()=>move(step.id,index-1)}><ArrowUp size={14}/></button><button className="kaus-chat-icon-action" aria-label={`${t("workbench.down")} ${index+1}`} disabled={busy||active||index===definition.steps.length-1} onClick={()=>move(step.id,index+1)}><ArrowDown size={14}/></button><button className="kaus-chat-icon-action" aria-label={`${t("workbench.deleteStep")} ${index+1}`} disabled={busy||active||definition.steps.length===1} onClick={()=>edit({...definition,steps:definition.steps.filter(value=>value.id!==step.id)})}><Trash2 size={14}/></button></header>
        <textarea aria-label={`${t("workbench.prompt")} ${index+1}`} maxLength={50000} value={step.prompt} disabled={busy||active} rows={4} onChange={event=>edit({...definition,steps:definition.steps.map(value=>value.id===step.id?{...value,prompt:event.target.value}:value)})}/>
        <footer>{["input","previous"].filter(value=>value!=="previous"||index>0).map(value=><button key={value} className="workflow-variable" disabled={busy||active} onClick={()=>edit({...definition,steps:definition.steps.map(item=>item.id===step.id?{...item,prompt:`${item.prompt}${item.prompt?"\n":""}{{${value}}}`}:item)})}>{t(value==="input"?"workbench.insertInput":"workbench.insertPrevious")}</button>)}</footer>
      </div>)}
      </div>
      <button className="workbench-subtle" disabled={!definition||busy||active||(definition.steps.length>=20)} onClick={()=>definition&&edit({...definition,steps:[...definition.steps,{id:key(),title:`${t("workbench.step")} ${definition.steps.length+1}`,prompt:"{{previous}}"}]})}><Plus size={16}/>{t("workbench.addStep")}</button>
      <label className="workflow-input">{t("workbench.input")}<textarea rows={3} maxLength={100000} value={input} disabled={busy||active} onChange={event=>{setInput(event.target.value);runKey.current=null;}} placeholder={t("workbench.optional")}/></label>
      <footer className="workflow-run-bar">{!configured?<button className="workbench-primary" onClick={onConfigure}>{t("workbench.configure")}<ChevronRight size={16}/></button>:<button className="workbench-primary" disabled={busy||active||!valid} onClick={()=>void start()}>{busy?<LoaderCircle size={16} className="kaus-ui-spin"/>:<Play size={16}/>} {t("workbench.run")}</button>}{dirty&&<span className="workbench-muted">{t("workbench.settingsChanged")}</span>}</footer>
      {failed&&<p role="alert" className="workbench-inline-error">{failed}</p>}
    </section>
    <section className="workflow-runs" aria-label={t("workbench.jobs")}><header><h2>{t("workbench.jobs")}</h2>{jobs.length>0&&<select aria-label={t("workbench.jobs")} value={job?.id??""} onChange={event=>setSelected(event.target.value)}>{jobs.map((value,index)=><option key={value.id} value={value.id}>{t((`workbench.status.${value.status}`) as DictKey)} · {jobs.length-index}</option>)}</select>}</header>
      {!job?<div className="workbench-empty">{t("workbench.noJobs")}</div>:<><div className="workflow-job-bar"><span className={`workbench-job-state is-${job.status}`}>{activeModuleJob(job)?<LoaderCircle size={14} className="kaus-ui-spin"/>:job.status==="completed"?<Check size={14}/>:null}{t((`workbench.status.${job.status}`) as DictKey)}</span>{activeModuleJob(job)?<button className="workbench-subtle" disabled={busy} onClick={()=>void mutate(()=>cancelModuleJob(module.id,job.id))}><Square size={14}/>{t("workbench.cancel")}</button>:["interrupted","failed"].includes(job.status)&&<button className="workbench-subtle" disabled={busy||active||!configured} onClick={()=>void mutate(()=>resumeModuleJob(module.id,job.id))}><Play size={14}/>{t("workbench.resume")}</button>}</div>
        {job.steps.map((step,index)=><article className={`workflow-result is-${step.status}`} key={step.id}><header><span className="workflow-step-number">{index+1}</span><strong>{step.title}</strong><span className="workbench-muted">{t((`workbench.status.${step.status}`) as DictKey)}</span>{step.conversationId&&<button className="workbench-subtle" onClick={()=>onConversation(step.conversationId!)}>{t(step.status==="waiting"?"workbench.approval":"workbench.details")}<ChevronRight size={14}/></button>}</header>{step.output&&<div className="workflow-output">{step.output}</div>}</article>)}
      </>}
    </section>
  </div>;
}
