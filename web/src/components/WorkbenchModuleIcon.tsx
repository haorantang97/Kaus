import { Boxes, Code2, FileText, PenLine, Workflow } from "lucide-react";
import type { WorkbenchModule } from "../lib/workbenchApi";

export function WorkbenchModuleIcon({ module, size = 20 }: { module: WorkbenchModule; size?: number }) {
  const icon = module.icon || "box";
  if (/^[a-zA-Z0-9_/-]+\.(svg|png|webp)$/.test(icon) && !icon.split("/").includes("..")) {
    return <img src={`/api/workbench/assets/${encodeURIComponent(module.id)}/${icon}`} alt="" width={size} height={size} style={{objectFit:"contain"}} />;
  }
  const Glyph = ({ workflow: Workflow, pen: PenLine, "pen-line": PenLine, file: FileText, code: Code2 })[icon] ?? Boxes;
  return <Glyph size={size} aria-hidden="true" />;
}
