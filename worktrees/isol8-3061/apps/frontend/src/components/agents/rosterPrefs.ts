"use client";

import { useSyncExternalStore } from "react";

/**
 * How much of the roster the owner wants to see, remembered per browser.
 *
 * The roster is collapsed by default: Friday is the product, and the rest of
 * the team is one click away rather than ten rows deep. `pinned` is the escape
 * hatch — an agent the owner works with often sits beside Friday permanently
 * instead of behind the expander.
 *
 * Same shape and the same reasoning as `teams/issues/tasksViewMode`: browser
 * state read through `useSyncExternalStore` so it is SSR-safe (the mobile shell
 * server-renders), keyed per browser rather than per company. Agent ids are
 * UUIDs, so a pin from another company simply matches nothing.
 */
export interface RosterPrefs {
  readonly pinned: ReadonlySet<string>;
  readonly expanded: boolean;
}

const KEY = "lightbulb:roster:prefs";
const DEFAULT: RosterPrefs = { pinned: new Set(), expanded: false };

const listeners = new Set<() => void>();

// useSyncExternalStore compares snapshots with Object.is, so getSnapshot MUST
// return the same object until the underlying value actually changes —
// re-parsing into a fresh object on every render would loop forever. Cache on
// the raw string and only rebuild when that string moves.
let cachedRaw: string | null = null;
let cachedValue: RosterPrefs = DEFAULT;

function readRaw(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    // Private mode / storage disabled — the defaults stand.
    return null;
  }
}

function parse(raw: string | null): RosterPrefs {
  if (!raw) return DEFAULT;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return DEFAULT;
    const { pinned, expanded } = parsed as { pinned?: unknown; expanded?: unknown };
    return {
      pinned: new Set(
        Array.isArray(pinned) ? pinned.filter((v): v is string => typeof v === "string") : [],
      ),
      expanded: expanded === true,
    };
  } catch {
    return DEFAULT;
  }
}

function subscribe(onStoreChange: () => void): () => void {
  listeners.add(onStoreChange);
  window.addEventListener("storage", onStoreChange);
  return () => {
    listeners.delete(onStoreChange);
    window.removeEventListener("storage", onStoreChange);
  };
}

function getSnapshot(): RosterPrefs {
  const raw = readRaw();
  if (raw === cachedRaw) return cachedValue;
  cachedRaw = raw;
  cachedValue = parse(raw);
  return cachedValue;
}

function write(next: RosterPrefs): void {
  try {
    localStorage.setItem(
      KEY,
      JSON.stringify({ pinned: [...next.pinned], expanded: next.expanded }),
    );
  } catch {
    // Nothing was stored, so the snapshot would read back the old value —
    // notifying would only cause a re-render that changes nothing.
    return;
  }
  for (const l of listeners) l();
}

export function toggleRosterPin(agentId: string): void {
  const current = getSnapshot();
  const pinned = new Set(current.pinned);
  if (!pinned.delete(agentId)) pinned.add(agentId);
  write({ ...current, pinned });
}

export function setRosterExpanded(expanded: boolean): void {
  write({ ...getSnapshot(), expanded });
}

export function useRosterPrefs(): RosterPrefs {
  return useSyncExternalStore(subscribe, getSnapshot, () => DEFAULT);
}
