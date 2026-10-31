"use client";

import { useSyncExternalStore } from "react";
import type { EngineRecord } from "@lightbulb/shared";
import { bridge, hasEngineAsk } from "./localTerminal";

export interface EngineDirectory { records: readonly EngineRecord[]; down: boolean }

const ABSENT: EngineDirectory = { records: [], down: false };
const POLL_MS = 5000;

let state = ABSENT;
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | undefined;
let run = 0;
let pending = false;

const set = (next: EngineDirectory) => {
  if (next.down === state.down && next.records.length === state.records.length && JSON.stringify(next.records) === JSON.stringify(state.records)) return;
  state = next;
  listeners.forEach((l) => l());
};

async function poll(mine: number) {
  if (pending) return;
  pending = true;
  const invoke = bridge();
  try {
    const answer = (await invoke!("engine_ask", { op: "directory" })) as { ok?: boolean; sessions?: EngineRecord[]; ended?: EngineRecord[] } | null;
    if (mine !== run) return;
    if (!answer || answer.ok === false) return set({ records: [], down: true });
    const records = [...(answer.sessions ?? []), ...(answer.ended ?? [])];
    // A 0.7.x engine sends no `wakes`: absent, not down.
    set(records.some((r) => typeof r.wakes !== "boolean") ? ABSENT : { records, down: false });
  } catch {
    if (mine === run) set({ records: [], down: true });
  } finally {
    if (mine === run) pending = false;
  }
}

async function start() {
  const mine = ++run;
  if (!(await hasEngineAsk()) || mine !== run) return;
  void poll(mine);
  timer = setInterval(() => void poll(mine), POLL_MS);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) void start();
  return () => {
    listeners.delete(listener);
    if (listeners.size) return;
    run++;
    pending = false;
    clearInterval(timer);
    timer = undefined;
    state = ABSENT;
  };
}

/** The engine's directory as this desktop app sees it; empty on the web, the phone and older apps. One poll, shared, while anything is subscribed. */
export function useEngineDirectory(): EngineDirectory {
  return useSyncExternalStore(subscribe, () => state, () => ABSENT);
}
