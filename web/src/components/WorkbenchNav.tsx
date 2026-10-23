import { WorkbenchModuleIcon } from "./WorkbenchModuleIcon";
import { useEffect, useState, type DragEvent } from "react";
import { ArrowDown, ArrowUp, LayoutGrid, MoreHorizontal, PinOff } from "lucide-react";
import { useLocale } from "../i18n";
import { notifyWorkbenchChanged, patchWorkbenchModule, reorderWorkbenchPins, type WorkbenchModule } from "../lib/workbenchApi";
import { workbenchPath } from "../lib/routes";
import "./workbench.css";
export const MODULE_DRAG_TYPE = "application/x-kaus-module";
export function writeModuleDrag(event: DragEvent, id: string) { event.dataTransfer.setData(MODULE_DRAG_TYPE, id); event.dataTransfer.effectAllowed = "move"; }
export function WorkbenchNav({modules, activeId, active, navigate, compact = false}: {modules: WorkbenchModule[]; activeId: string | null; active: boolean; navigate: (path: string) => void; compact?: boolean}) {
  const {t} = useLocale();
  const [menu, setMenu] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(()=> {
    if (!menu) return;
    const outside=(event:PointerEvent)=>{if(!(event.target as Element)?.closest(".workbench-menu"))setMenu(null);};
    const escape=(event:KeyboardEvent)=>{if(event.key==="Escape")setMenu(null);};
    document.addEventListener("pointerdown",outside);document.addEventListener("keydown",escape);
    return ()=>{document.removeEventListener("pointerdown",outside);document.removeEventListener("keydown",escape);};
  },[menu]);
  const pins = modules.filter(module => module.pinned && module.enabled).sort((a,b) => a.pinOrder - b.pinOrder);
  async function drop(id: string, before?: string) {
    const module = modules.find(item => item.id === id && item.enabled);
    if (!module) return;
    const next = pins.map(item => item.id).filter(item => item !== id);
    const index = before ? next.indexOf(before) : -1;
    next.splice(index < 0 ? next.length : index, 0, id);
    try { await reorderWorkbenchPins(next); notifyWorkbenchChanged(); setFailed(false); } catch { setFailed(true); }
  }
  async function move(id: string, offset: number) {
    const next = pins.map(item => item.id), index = next.indexOf(id), target = index + offset;
    if (target < 0 || target >= next.length) return;
    [next[index],next[target]] = [next[target],next[index]];
    try { await reorderWorkbenchPins(next); notifyWorkbenchChanged(); setFailed(false); } catch { setFailed(true); }
  }
  const dragOver = (event: DragEvent, id: string) => { if (event.dataTransfer.types.includes(MODULE_DRAG_TYPE)) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; setOver(id); } };
  return <div className={`workbench-nav ${compact ? "is-compact" : ""} ${over === "end" ? "is-drop" : ""}`} aria-label={t("workbench.drop")}
    onDragOver={event => dragOver(event,"end")} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setOver(null); }}
    onDrop={event => { event.preventDefault(); setOver(null); void drop(event.dataTransfer.getData(MODULE_DRAG_TYPE)); }}>
    <button className={`shell-nav-item ${active && !activeId ? "is-active" : ""}`} title={t("workbench.title")} aria-label={t("workbench.title")} onClick={() => navigate(workbenchPath())}><span className="shell-nav-icon"><LayoutGrid /></span>{!compact && <span className="shell-nav-label">{t("workbench.title")}</span>}</button>
    {pins.map((module,index) => <div key={module.id} className={`workbench-pin ${over === module.id ? "is-drop" : ""}`} draggable onDragStart={event => writeModuleDrag(event,module.id)} onDragEnd={() => setOver(null)}
      onDragOver={event => { event.stopPropagation(); dragOver(event,module.id); }} onDrop={event => { event.preventDefault(); event.stopPropagation(); setOver(null); void drop(event.dataTransfer.getData(MODULE_DRAG_TYPE),module.id); }}>
      <button className={`shell-nav-item ${activeId === module.id ? "is-active" : ""}`} title={module.name} aria-label={module.name} onClick={() => navigate(workbenchPath(module.id))}><span className="shell-nav-icon"><WorkbenchModuleIcon module={module} /></span>{!compact && <span className="shell-nav-label">{module.name}</span>}</button>
      {!compact && <button className="workbench-pin-menu kaus-chat-icon-action" aria-label={`${module.name} · ${t("common.more")}`} onClick={() => setMenu(menu === module.id ? null : module.id)}><MoreHorizontal size={15}/></button>}
      {menu === module.id && <div className="workbench-menu" role="menu" onKeyDown={event => { if (event.key === "Escape") setMenu(null); }}>
        <button role="menuitem" disabled={index===0} onClick={() => { setMenu(null); void move(module.id,-1); }}><ArrowUp size={14}/>{t("workbench.up")}</button>
        <button role="menuitem" disabled={index===pins.length-1} onClick={() => { setMenu(null); void move(module.id,1); }}><ArrowDown size={14}/>{t("workbench.down")}</button>
        <button role="menuitem" onClick={() => { setMenu(null); void patchWorkbenchModule(module.id,{pinned:false}).then(notifyWorkbenchChanged).catch(() => setFailed(true)); }}><PinOff size={14}/>{t("workbench.unpin")}</button>
      </div>}
    </div>)}
    {failed && <span role="status" className="workbench-inline-error">{t("workbench.actionFailed")}</span>}
  </div>;
}
