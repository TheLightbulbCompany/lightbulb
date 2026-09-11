"use client";

import { useState } from "react";

/**
 * Shared pending/error plumbing for a header/details action that can fail
 * (star, mute, rename, leave, ...). One `run` per component instance is
 * enough -- only one such action is ever in flight from a given control at
 * a time, so a single pending/error pair covers all of them.
 */
export function useAsyncAction() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string>();

  async function run(action: () => Promise<void>, fallback: string, onDone?: () => void) {
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
  }

  return { pending, error, setError, run };
}
