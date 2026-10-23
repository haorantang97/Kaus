import { useCallback, useEffect, useState } from "react";
import { fetchWorkbenchModules, WORKBENCH_CHANGED, type WorkbenchModule } from "../lib/workbenchApi";
export function useWorkbench(enabled: boolean) {
  const [modules, setModules] = useState<WorkbenchModule[]>([]);
  const [loading, setLoading] = useState(enabled);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  useEffect(() => { window.addEventListener(WORKBENCH_CHANGED, refresh); return () => window.removeEventListener(WORKBENCH_CHANGED, refresh); }, [refresh]);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setLoading(true);
    void fetchWorkbenchModules(controller.signal).then(data => { if (!Array.isArray(data.modules)) throw new Error("invalid_modules"); if (!controller.signal.aborted) { setModules(data.modules); setFailed(false); } })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [enabled, revision]);
  return {modules, loading, failed, refresh};
}
