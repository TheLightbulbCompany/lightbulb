import { useEffect, useRef } from "react";

/**
 * Consume a one-shot navigation param.
 *
 * Fires `onValue(value)` once per distinct non-empty value, then calls `clear`
 * to drop it from the route. This exists because expo-router tab screens stay
 * MOUNTED across navigations — re-navigating to the same screen only updates
 * params, so the param is the sole signal a fresh navigation happened. The ref
 * guard blocks a double-fire when `onValue`/`clear` change identity while the
 * value is momentarily unchanged; `clear` (setting the param back to undefined)
 * is what lets the SAME value re-fire on a later navigation instead of being
 * swallowed as "unchanged".
 */
export function useNavParamConsumer(
  value: string | undefined,
  onValue: (value: string) => void,
  clear: () => void,
): void {
  const lastConsumed = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (value === undefined || value === "") {
      lastConsumed.current = undefined;
      return;
    }
    if (lastConsumed.current === value) return;
    lastConsumed.current = value;
    onValue(value);
    clear();
  }, [value, onValue, clear]);
}
