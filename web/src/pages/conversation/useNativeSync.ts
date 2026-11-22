import { useCallback, useEffect, useRef, useState } from "react";

import { syncNativeHistory } from "../../lib/sessionApi";

const MIN_INTERVAL_MS = 5000;

/** Out-of-band detection (2026-11-22「带外检测」). When the window comes back, ask the
 *  kernel whether the native session moved on outside Kaus. Merges arrive over SSE.
 *  If a Kaus turn is running, `conflict` asks the user: merge after it, or stop it. */
export function useNativeSync({ conversationId, running, external, archived, ready }: {
  conversationId: string;
  running: boolean;
  external: boolean;
  archived: boolean;
  ready: boolean;
}) {
  const [conflict, setConflict] = useState(false);
  const deferred = useRef(false);
  const last = useRef(0);
  const blocked = !ready || external || archived;

  const check = useCallback(async (force = false) => {
    if (blocked) return;
    const now = Date.now();
    if (!force && now - last.current < MIN_INTERVAL_MS) return;
    last.current = now;
    const result = await syncNativeHistory(conversationId).catch(() => null);
    if (!result) return;
    if (result.status === "running") setConflict(true);
    else setConflict(false);
  }, [blocked, conversationId]);

  useEffect(() => { setConflict(false); deferred.current = false; last.current = 0; }, [conversationId]);

  useEffect(() => {
    void check(true);
    // A desktop window can stay "visible" behind the terminal, so focus counts too.
    const onReturn = () => { if (document.visibilityState === "visible") void check(); };
    document.addEventListener("visibilitychange", onReturn);
    window.addEventListener("focus", onReturn);
    return () => { document.removeEventListener("visibilitychange", onReturn); window.removeEventListener("focus", onReturn); };
  }, [check]);

  useEffect(() => {
    if (!running && deferred.current) { deferred.current = false; void check(true); }
  }, [running, check]);

  return {
    conflict,
    /** Merge once the current turn ends. */
    later: () => { deferred.current = true; setConflict(false); },
  };
}
