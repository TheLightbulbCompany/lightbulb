"use client";

// The owner's voice surfaces read three things: the timeline (calls + meetings), the assistant's settings
// (one JSON document, GET/PUT /voice/agent) and the open Questions (/needs-you). Shapes: routers/voice.py.

import { useCallback } from "react";
import useSWR from "swr";
import type { QuestionView } from "@lightbulb/shared";
import { useApi } from "@/lib/api";

export type Outcome = "booked" | "moved" | "cancelled" | "message" | "answered" | "needs_you" | "handoff_missed" | "spam";

export interface CallItem {
  type: "call"; roomId: string; at: number;
  caller?: string; callerName?: string; returning?: boolean; durationSec?: number;
  outcome?: Outcome; summary?: string; booking?: string;
  /** What the assistant did, step by step: `t` seconds into the call. */
  steps?: { t?: number; text: string; guessed?: boolean }[];
  transcript?: { who: "caller" | "assistant"; text: string }[];
  callerMemory?: string[]; recordingUrl?: string;
  /** The owner calling their own line (owner mode): a "Yours" row. */
  ownerMode?: boolean; resolvedAt?: string;
}

export interface MeetingItem {
  type: "meeting"; roomId: string; at: number;
  title?: string; startAt?: number; endAt?: number; status?: string; people?: string[]; summary?: string;
  asked?: { t?: number; who?: string; q: string; a?: string }[];
  followUps?: { text: string; status?: string }[];
}

export type TimelineItem = CallItem | MeetingItem;

export function useTimeline() {
  const api = useApi();
  // ponytail: a 20 s poll, not a push; subscribe to the room events when calls must land live.
  return useSWR("/voice/timeline", async () => ((await api.get("/voice/timeline")) as { items: TimelineItem[] }).items,
    { refreshInterval: 20_000 });
}

export interface Knowledge { q: string; a: string; source?: string }
export type Day = { open: string; close: string; closed?: boolean };

export interface AgentConfig {
  name: string; greeting: string; voice: string; spanish: boolean; live: boolean; paused: boolean;
  allow: { book: boolean; move: boolean; waitlist: boolean; greetByName: boolean; answer: boolean };
  knowledge: Knowledge[]; toAdd: string[]; instructions: string;
  hours: Record<string, Day>; holidays: string;
  services: { name: string; minutes: number; price: string; staff: string }[]; bufferMin: number;
  bookingsGo: "google" | "square" | "other" | "own"; otherSystem: string;
  number: string; forwardFrom: string;
  handoff: { number: string; onlyOpenHours: boolean };
  meetings: { followUps: boolean; emailNotes: boolean };
  reminders: { dayBefore: boolean; missedCallers: boolean; waitlist: boolean };
}

export const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

export const DEFAULT_CONFIG: AgentConfig = {
  name: "", greeting: "", voice: "Warm", spanish: false, live: false, paused: false,
  allow: { book: true, move: true, waitlist: true, greetByName: true, answer: true },
  knowledge: [], toAdd: [], instructions: "",
  hours: Object.fromEntries(DAYS.map((d) => [d, { open: "09:00", close: "18:00", closed: d === "Sun" }])),
  holidays: "",
  services: [], bufferMin: 10,
  bookingsGo: "own", otherSystem: "",
  number: "", forwardFrom: "",
  handoff: { number: "", onlyOpenHours: true },
  meetings: { followUps: true, emailNotes: true },
  reminders: { dayBefore: true, missedCallers: true, waitlist: true },
};

/** The name callers hear; "Lightbulb" until the owner picks one. */
export const nameOf = (c?: Partial<AgentConfig> | null) => c?.name?.trim() || "Lightbulb";

export function useAgentConfig() {
  const api = useApi();
  const swr = useSWR("/voice/agent", async () => {
    const { config } = (await api.get("/voice/agent")) as { config: Partial<AgentConfig> | null };
    return { ...DEFAULT_CONFIG, ...config } as AgentConfig;
  });
  const save = useCallback(async (next: AgentConfig) => {
    await swr.mutate(next, { revalidate: false });
    await api.put("/voice/agent", { config: next });
  }, [api, swr]);
  return { ...swr, save };
}

/** Open Questions for the viewer. A workspace without the super-agent 404s: no Questions, not an error. */
export function useQuestions() {
  const api = useApi();
  return useSWR("/needs-you", async () => {
    try { return ((await api.get("/needs-you")) as { items: QuestionView[] }).items; }
    catch (e) { if ((e as { status?: number }).status === 404) return []; throw e; }
  }, { refreshInterval: 30_000 });
}

/** A call still waiting on the owner: the assistant didn't know, or a caller wanted a person. */
export const callNeedsYou = (i: TimelineItem): i is CallItem =>
  i.type === "call" && !i.resolvedAt && (i.outcome === "needs_you" || i.outcome === "handoff_missed");

export const mmss = (sec?: number) => sec == null ? "" : `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}`;
export const who = (c: CallItem) => c.callerName || c.caller || "Unknown caller";
