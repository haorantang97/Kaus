import { apiPost } from "./api";
import { toast, undoToast } from "../components/ui";
import { describeFailure } from "./sessionApi";

/** Change a project's parent at once and offer 撤销 (2026-11-18: reversible, so no confirm first). */
export async function moveProjectWithUndo({ node, from, to, message, onChanged }: {
  node: string;
  /** Current parent; `null` is top level. */
  from: string | null;
  to: string | null;
  message: string;
  onChanged: () => void;
}): Promise<boolean> {
  if (from === to) return false;
  try {
    await apiPost("/api/move", { node, new_parent: to });
  } catch (failure) {
    toast(describeFailure(failure), "bad");
    return false;
  }
  onChanged();
  undoToast(message, async () => {
    try { await apiPost("/api/move", { node, new_parent: from }); onChanged(); }
    catch (failure) { toast(describeFailure(failure), "bad"); }
  });
  return true;
}
