"use client";

import { useSyncExternalStore } from "react";

/**
 * Which view the Agents panel opens on, remembered per browser.
 *
 * The roster is the default and the org chart is the second view of the same
 * `/agents` read (the retired `teams-org-chart` key opens it — see
 * panelRegistry's RETIRED_PANEL_ALIASES). Persistence is what keeps an owner
 * who lives in the chart from re-picking it on every reload.
 *
 * Same shape as `issues/tasksViewMode.ts` (and `hooks/useIsDesktop`):
 * localStorage is external state, and the `useSyncExternalStore` server
 * snapshot is what keeps it SSR-safe. Deliberately a sibling rather than a
 * shared generic — one storage key, one value pair, no configuration.
 *
 * Keyed per browser, NOT per company: a list-vs-chart preference is a property
 * of the person, not the tenant, and carries no tenant data.
 */
export type AgentsViewMode = "list" | "chart";

const KEY = "paperclip:agents:view-mode";
const DEFAULT: AgentsViewMode = "list";

const listeners = new Set<() => void>();

function subscribe(onStoreChange: () => void): () => void {
  listeners.add(onStoreChange);
  // `storage` only fires in OTHER tabs, so same-tab writes go through the
  // listener set above; this keeps a second tab in step.
  window.addEventListener("storage", onStoreChange);
  return () => {
    listeners.delete(onStoreChange);
    window.removeEventListener("storage", onStoreChange);
  };
}

// Returns a primitive, so useSyncExternalStore's identity check is a value
// comparison and re-reading storage on every render cannot loop.
function getSnapshot(): AgentsViewMode {
  try {
    const raw = localStorage.getItem(KEY);
    return raw === "list" || raw === "chart" ? raw : DEFAULT;
  } catch {
    // Private mode / storage disabled — the default stands.
    return DEFAULT;
  }
}

export function setAgentsViewMode(mode: AgentsViewMode): void {
  try {
    localStorage.setItem(KEY, mode);
  } catch {
    // Non-fatal, but then nothing changed, so don't pretend it did by
    // notifying: the snapshot would read back the old value anyway.
  }
  for (const l of listeners) l();
}

export function useAgentsViewMode(): AgentsViewMode {
  return useSyncExternalStore(subscribe, getSnapshot, () => DEFAULT);
}
