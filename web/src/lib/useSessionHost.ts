import { useCallback, useEffect, useState } from "react";
import { bootstrapSessionAuth, SessionUnavailableError } from "./sessionApi";

export type SessionHostStatus = "probing" | "available" | "unavailable";

/** Connection readiness never selects a different version of the interface. */
export function useSessionHost() {
  const [status, setStatus] = useState<SessionHostStatus>("probing");
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => {
    setStatus("probing");
    setError(null);
    setAttempt(value => value + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let retries = 0;
    const connect = async () => {
      try {
        await bootstrapSessionAuth();
        if (!cancelled) setStatus("available");
      } catch (failure) {
        if (cancelled) return;
        const transient = !(failure instanceof SessionUnavailableError)
          || failure.status === 0 || failure.status >= 500;
        if (transient && retries < 2) {
          timer = setTimeout(connect, [500, 1500][retries++]);
          return;
        }
        setError(String((failure as Error)?.message ?? failure));
        setStatus("unavailable");
      }
    };
    void connect();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [attempt]);

  useEffect(() => {
    if (status !== "unavailable") return;
    window.addEventListener("online", retry);
    return () => window.removeEventListener("online", retry);
  }, [status, retry]);

  return { status, error, retry };
}
