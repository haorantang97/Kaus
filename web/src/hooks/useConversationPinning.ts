import { useCallback, useRef } from "react";

export const SCROLL_INPUT_COOLDOWN_MS = 150;
/** Keep a token/resize callback from overtaking a wheel gesture before its scroll event. */
export function useScrollInputCooldown() {
  const lastInput = useRef(-Infinity);
  const markInput = useCallback(() => { lastInput.current = performance.now(); }, []);
  const canFollow = useCallback(() => performance.now() - lastInput.current > SCROLL_INPUT_COOLDOWN_MS, []);
  const reset = useCallback(() => { lastInput.current = -Infinity; }, []);
  return { markInput, canFollow, reset };
}
