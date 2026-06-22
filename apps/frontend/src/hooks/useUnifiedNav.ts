"use client";

import { useEffect, useSyncExternalStore } from "react";
import { useSearchParams } from "next/navigation";
import { applyUnifiedNavParam, readUnifiedNav, resolveUnifiedNav } from "@/lib/unifiedNav";

// Module-level subscriber set so the localStorage read can be re-driven both by
// other tabs (the native `storage` event) and by our own URL-param writes.
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  if (typeof window !== "undefined") {
    window.addEventListener("storage", onChange);
  }
  return () => {
    listeners.delete(onChange);
    if (typeof window !== "undefined") {
      window.removeEventListener("storage", onChange);
    }
  };
}

// Returns whether the unified nav is enabled in this browser. `?unifiedNav=1`
// sets it (sticky), `?unifiedNav=0` clears it. `getServerSnapshot` is `false`,
// so the server render and the hydrating client render agree — no hydration
// mismatch — then `useSyncExternalStore` switches to the live localStorage
// value post-hydration.
export function useUnifiedNav(): boolean {
  const searchParams = useSearchParams();
  const param = searchParams.get("unifiedNav");

  const stored = useSyncExternalStore(subscribe, readUnifiedNav, () => false);

  // Persist the URL opt-in/out for future param-less visits. Writing to
  // localStorage is a genuine external-system sync (what effects are for) and
  // sets no React state, so it does not trip `react-hooks/set-state-in-effect`.
  // Notify subscribers so the store re-reads after the write.
  useEffect(() => {
    if (applyUnifiedNavParam(param) !== null) emit();
  }, [param]);

  // Resolve in render so an explicit `?unifiedNav=0|1` overrides the stored
  // value SYNCHRONOUSLY — an opt-out is honored on the first render, before the
  // effect above persists it, so consumers never mount the inline branch on a
  // stale `true`.
  return resolveUnifiedNav(param, stored);
}
