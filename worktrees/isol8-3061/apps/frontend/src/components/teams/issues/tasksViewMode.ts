"use client";

import { useSyncExternalStore } from "react";

/**
 * Which view the Tasks surface opens on, remembered per browser.
 *
 * Board is the default. That makes persistence load-bearing rather than
 * decorative: while list was the default it was also where most people stayed,
 * so the absence of persistence was invisible. Flip the default without this
 * and everyone who prefers list re-picks it on every reload.
 *
 * Modelled on `hooks/useIsDesktop` — localStorage is external state, and the
 * `useSyncExternalStore` server snapshot is what keeps it SSR-safe. The obvious
 * alternatives are both wrong here: a `useState` initializer that reads storage
 * hydrates a tree the server never sent, and restoring via `setState` in an
 * effect is the same mismatch one frame later (and trips
 * `react-hooks/set-state-in-effect`).
 *
 * Keyed per browser, NOT per company. `TasksList` and `IssuesPanel` are both
 * prop-driven with no company context, and threading one down purely for this
 * would be a larger change than the feature — while a list-vs-board preference
 * is a property of the person, not the tenant, and carries no tenant data.
 */
export type TasksViewMode = "list" | "board";

const KEY = "paperclip:tasks:view-mode";
const DEFAULT: TasksViewMode = "board";

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
function getSnapshot(): TasksViewMode {
  try {
    const raw = localStorage.getItem(KEY);
    return raw === "list" || raw === "board" ? raw : DEFAULT;
  } catch {
    // Private mode / storage disabled — the default stands.
    return DEFAULT;
  }
}

export function setTasksViewMode(mode: TasksViewMode): void {
  try {
    localStorage.setItem(KEY, mode);
  } catch {
    // Non-fatal, but then nothing changed, so don't pretend it did by
    // notifying: the snapshot would read back the old value anyway.
  }
  for (const l of listeners) l();
}

export function useTasksViewMode(): TasksViewMode {
  return useSyncExternalStore(subscribe, getSnapshot, () => DEFAULT);
}
