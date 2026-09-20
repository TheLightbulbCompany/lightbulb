"use client";

import { useCallback, useState } from "react";

/**
 * Shared pending/error plumbing for a header/details action that can fail
 * (star, mute, rename, leave, ...). One `run` per component instance is
 * enough -- only one such action is ever in flight from a given control at
 * a time, so a single pending/error pair covers all of them. Because the
 * pair is shared, a caller with more than one surface that can show `error`
 * (e.g. an inline banner AND a dialog) MUST call `reset()` on every
 * transition between them -- otherwise a failure from one surface leaks
 * into the next one the viewer opens.
 */
export function useAsyncAction() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string>();

  function reset() { setError(undefined); }

  const run = useCallback(async (action: () => Promise<void>, fallback: string, onDone?: () => void) => {
    setPending(true);
    setError(undefined);
    try {
      await action();
      onDone?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : fallback);
    } finally {
      setPending(false);
    }
  }, []);

  return { pending, error, setError, reset, run };
}
