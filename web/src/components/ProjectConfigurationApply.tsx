import { useState } from "react";
import { SlidersHorizontal } from "lucide-react";
import { useLocale } from "../i18n";
import type { BindingWire } from "../lib/sessionApi";
import { backendDisplay } from "../lib/backend-display";
import { ProjectAgentPicker } from "./ProjectAgentPicker";
import { MaterializeModal } from "./MaterializeModal";
import { Button, Modal, ModalTitle } from "./ui";

/** Optional native file projection, separate from global Agent registration. */
export function ProjectConfigurationApply({ projectId }: { projectId: string }) {
  const { locale } = useLocale();
  const zh = locale === "zh";
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState(false);
  const [binding, setBinding] = useState<BindingWire | null>(null);
  const label = zh ? "应用项目配置" : "Apply project configuration";
  const close = () => { setOpen(false); setPreview(false); setBinding(null); };
  return <>
    <Button aria-label={label} title={label} onClick={() => setOpen(true)}><SlidersHorizontal size={15} /></Button>
    {open && !preview && <Modal onClose={close} width={420}>
      <ModalTitle>{label}</ModalTitle>
      <ProjectAgentPicker projectId={projectId} value={binding?.id} onChange={setBinding} />
      <div className="mt-4 flex justify-end"><Button disabled={!binding} onClick={() => setPreview(true)}>{zh ? "预览变更" : "Preview changes"}</Button></div>
    </Modal>}
    {open && preview && binding && <MaterializeModal bindingId={binding.id}
      displayName={backendDisplay(binding.backendId).displayName} onClose={close} />}
  </>;
}
