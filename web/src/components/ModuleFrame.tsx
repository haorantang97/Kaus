import { useLayoutEffect, useRef, useState } from "react";
import { RotateCw } from "lucide-react";
import { connectModuleFrame } from "../lib/workbenchBridge";
import type { WorkbenchModule } from "../lib/workbenchApi";
import { confirmAsync } from "./ui";
import { copyText } from "./cards/clipboard";
import { useLocale } from "../i18n";
export function ModuleFrame({module, onConversation, onConfigure}: {module: WorkbenchModule; onConversation: (id:string)=>void; onConfigure?:()=>void}) {
  const {t,locale} = useLocale();
  const ref = useRef<HTMLIFrameElement>(null), callback = useRef(onConversation), configure = useRef(onConfigure);
  callback.current = onConversation; configure.current = onConfigure;
  const [key,setKey] = useState(0), [failure,setFailure] = useState<"load"|"revoked"|null>(null);
  const loaded = useRef(false);
  useLayoutEffect(()=> {
    const frame=ref.current; if(!frame)return;
    loaded.current=false;setFailure(null);
    const disconnect=connectModuleFrame({frame,moduleId:module.id,onReady:()=>setFailure(previous=>previous==="load"?null:previous),onRevoked:()=>setFailure("revoked"),onConversation:id=>callback.current(id),ui:{
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
    return ()=> {disconnect();frame.removeEventListener("error",onError);window.clearTimeout(timer);};
  },[module.id,module.assetUrl,key,locale,module.name]);
  const asset = module.assetUrl && /^\/api\/workbench\/assets\/[^/]+\//.test(module.assetUrl) ? module.assetUrl : null;
  return <div className="workbench-frame-wrap">
    {asset && <iframe key={key} ref={ref} title={module.name} src={asset} sandbox="allow-scripts allow-forms" referrerPolicy="no-referrer" className="workbench-frame" onLoad={()=>{loaded.current=true;setFailure(previous=>previous==="load"?null:previous);}} />}
    {(!asset || failure)&&<div className="workbench-frame-status"><span>{t("workbench.frameFailed")}</span><button className="kaus-chat-icon-action" aria-label={t("workbench.retry")} onClick={()=>setKey(value=>value+1)}><RotateCw size={16}/></button></div>}
  </div>;
}
