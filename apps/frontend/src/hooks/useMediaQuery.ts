"use client";

import { useCallback, useSyncExternalStore } from "react";

/** A CSS media query's match, live; false on the server and where matchMedia is missing. */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback((callback: () => void) => {
    const media = window.matchMedia?.(query);
    media?.addEventListener("change", callback);
    return () => media?.removeEventListener("change", callback);
  }, [query]);
  return useSyncExternalStore(subscribe, () => !!window.matchMedia?.(query)?.matches, () => false);
}
