import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { RotateCw } from "lucide-react";
import { connectModuleFrame } from "../lib/workbenchBridge";
import { connectChatCutSurface } from "../lib/chatcutSurface";
import { openModuleFrame, releaseModuleFrame, renewModuleFrame, type WorkbenchModule } from "../lib/workbenchApi";
import { confirmAsync } from "./ui";
import { copyText } from "./cards/clipboard";
import { useLocale } from "../i18n";
const FRAME_RENEW_MS = 5 * 60_000;
const localAsset = (url: string | null | undefined) => url && /^\/api\/workbench\/assets\/[^/]+\//.test(url) ? url : null;
const grantedFrame = (url: string, moduleId: string) => url.startsWith(`/api/workbench/assets/${moduleId}/~`) ? url : null;
export function ModuleFrame({module, onConversation, onConfigure}: {module: WorkbenchModule; onConversation: (id:string)=>void; onConfigure?:()=>void}) {
  const {t,locale} = useLocale();
  const ref = useRef<HTMLIFrameElement>(null), callback = useRef(onConversation), configure = useRef(onConfigure);
  const currentModule=useRef(module);currentModule.current=module;
  callback.current = onConversation; configure.current = onConfigure;
  const [key,setKey] = useState(0), [failure,setFailure] = useState<"load"|"revoked"|null>(null);
  const loaded = useRef(false);
  const local = localAsset(module.assetUrl);
  const [frameUrl,setFrameUrl] = useState<string|null>(null);
  // Each open (and each retry) gets its own short-lived asset capability; it is renewed while shown.
  useEffect(()=> {
    setFrameUrl(null);
    if(!local)return;
    let active=true, token:string|null=null;
    void openModuleFrame(module.id).then(grant=>{
      const url=grantedFrame(grant.url,module.id);
      if(!active){void releaseModuleFrame(module.id,grant.token).catch(()=>undefined);return;}
      if(!url){setFailure("load");return;}
      token=grant.token;setFrameUrl(url);
    }).catch(()=>{if(active)setFailure("load");});
    const renew=window.setInterval(()=>{if(token)void renewModuleFrame(module.id,token).catch(()=>undefined);},FRAME_RENEW_MS);
    return ()=>{active=false;window.clearInterval(renew);if(token)void releaseModuleFrame(module.id,token).catch(()=>undefined);};
  },[module.id,local,key]);
  useLayoutEffect(()=> {
    const frame=ref.current; if(!frame)return;
    loaded.current=false;setFailure(null);
    const editor=connectChatCutSurface(frame,module.id,()=>Boolean(currentModule.current.enabled && currentModule.current.grants?.includes("editor:chatcut")));
    const disconnect=connectModuleFrame({frame,moduleId:module.id,onReady:()=>setFailure(previous=>previous==="load"?null:previous),onRevoked:()=>{editor?.dispose();setFailure("revoked");},onConversation:id=>callback.current(id),ui:{
      chatcut:editor?.request,
      configure: ()=>configure.current?.(),
      confirm: message=>confirmAsync(`${module.name} · ${message}`),
      copy: content=>copyText(content),
      external: async url=>{
        const host=new URL(url).hostname;
        if (!await confirmAsync(locale==="zh"?`打开 ${host}？`:`Open ${host}?`)) return false;
        window.open(url,"_blank","noopener,noreferrer");return true;
      },
    }});
    // Static modules and modules that connect on first action have no eager SDK handshake.
    const onError=()=>setFailure("load");
    frame.addEventListener("error",onError);
    const timer=window.setTimeout(()=>{if(!loaded.current)setFailure(previous=>previous??"load");},10_000);
    return ()=> {editor?.dispose();disconnect();frame.removeEventListener("error",onError);window.clearTimeout(timer);};
  },[module.id,frameUrl,key,locale,module.name]);
  return <div className="workbench-frame-wrap">
    {frameUrl && <iframe key={frameUrl} ref={ref} title={module.name} src={frameUrl} sandbox="allow-scripts allow-forms" referrerPolicy="no-referrer" className="workbench-frame" onLoad={()=>{loaded.current=true;setFailure(previous=>previous==="load"?null:previous);}} />}
    {(!local || failure)&&<div className="workbench-frame-status"><span>{t("workbench.frameFailed")}</span><button className="kaus-chat-icon-action" aria-label={t("workbench.retry")} onClick={()=>{setFrameUrl(null);setKey(value=>value+1);}}><RotateCw size={16}/></button></div>}
  </div>;
}
