import {useEffect,useState} from "react";
import {createRoot} from "react-dom/client";
import {WorkflowEditor} from "./WorkflowEditor";
import {configure,embedded,fetchContext,openConversation,workbenchFailure,type WorkbenchModule} from "./hostApi";
import {setModuleLocale} from "./i18n";
import "./style.css";
function App(){
 const [module,setModule]=useState<WorkbenchModule|null>(embedded()?null:{id:"workflow-studio",projectId:null,bindingId:null,grants:[]}),[error,setError]=useState("");
 useEffect(()=>{if(!embedded())return;let live=true;const refresh=async()=>{try{const context=await fetchContext();if(!live)return;document.body.classList.toggle("dark",context.theme==="dark");setModuleLocale(context.locale??"zh");setModule({...context,id:context.moduleId});setError("");}catch(error){if(live)setError(workbenchFailure(error));}};void refresh();const timer=window.setInterval(()=>void refresh(),2000);return()=>{live=false;clearInterval(timer);};},[]);
 async function settings(){if(!embedded()){setError("请从 Kaus 工作台打开模块后配置 Agent。");return;}try{await configure();}catch(error){setError(workbenchFailure(error));}}
 return <div className="sample-shell">{error&&<p role="alert" className="workbench-inline-error">{error}</p>}{module?<WorkflowEditor module={module} onConfigure={()=>void settings()} onConversation={id=>void openConversation(id).catch(error=>setError(workbenchFailure(error)))}/>:<div className="workbench-empty">正在连接…</div>}</div>;
}
createRoot(document.getElementById("root")!).render(<App/>);
