"use client";

// The owner's voice surfaces read three things: the timeline (calls + meetings), the assistant's settings
// (one JSON document, GET/PUT /voice/agent) and the open Questions (/needs-you). Shapes: routers/voice.py.

import { useCallback } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import type { QuestionView } from "@lightbulb/shared";
import { useApi } from "@/lib/api";

export type Outcome = "booked" | "moved" | "cancelled" | "message" | "answered" | "needs_you" | "handoff_missed" | "spam" | "silent" | "forwarded";

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
  title?: string; startAt?: number; endAt?: number; status?: string; inCallAt?: number; endedAt?: number; lastBotStatus?: string; people?: string[]; summary?: string;
  asked?: { t?: number; who?: string; q: string; a?: string }[];
  followUps?: { id?: string; text: string; status?: string; taskId?: string }[];
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
  /** `minutesGuessed`: the onboarding draft guessed the duration; cleared when the owner sets it. */
  services: { name: string; minutes: number; price: string; staff: string; minutesGuessed?: boolean }[]; bufferMin: number;
  /** IANA zone the hours are in; Onboarding saves the browser's. The server defaults to America/New_York. */
  timezone?: string;
  bookingsGo: "google" | "square" | "other" | "own"; otherSystem: string;
  number: string; forwardFrom: string;
  /** Set by the server when the forwarding check's call came back to the line (POST /voice/forward-check). */
  forwardingOk?: boolean;
  /** Onboarding step 4, stored by POST /voice/number; Go live reads it. */
  numberChoice?: { path: "keep" | "new"; areaCode?: string | null; carrier?: string | null; mode?: "missed" | "all" | null };
  handoff: { number: string; onlyOpenHours: boolean };
  meetings: { followUps: boolean; emailNotes: boolean };
  reminders: { dayBefore: boolean; missedCallers: boolean; waitlist: boolean };
  /** Past the included minutes: keep answering at the overage rate, or send calls to the owner's phone. */
  runOut?: "keep" | "forward";
  /** Onboarding: what the owner typed in step 1, and the step to resume at (absent once it went live). */
  business?: string; city?: string; website?: string; onboardingStep?: number;
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
  reminders: { dayBefore: true, missedCallers: false, waitlist: false },
};

/** In the app the assistant is always Lightbulb (founder, 8 Nov 2026); the Agent-page name is only what callers hear. */
export const LIGHTBULB = "Lightbulb";

/** The name callers hear; "Lightbulb" until the owner picks one. */
export const nameOf = (c?: Partial<AgentConfig> | null) => c?.name?.trim() || LIGHTBULB;

/** The Agent page document, defaults filled in. `isNew`: the workspace has no document yet (first run). */
export function useAgentConfig() {
  const api = useApi();
  const swr = useSWR("/voice/agent", async () => {
    const { config } = (await api.get("/voice/agent")) as { config: Partial<AgentConfig> | null };
    return { config: { ...DEFAULT_CONFIG, ...config } as AgentConfig, isNew: config == null };
  });
  const { mutate } = swr;
  const save = useCallback(async (next: AgentConfig) => {
    await mutate({ config: next, isNew: false }, { revalidate: false });
    await api.put("/voice/agent", { config: next });
  }, [api, mutate]);
  return { data: swr.data?.config, isNew: swr.data?.isNew, error: swr.error, isLoading: swr.isLoading, save };
}

/** The workspace's own team address, the one a Meet invite reaches (GET /agent-mail/address `invite_address`). */
export function useInviteAddress(): string | undefined {
  const api = useApi();
  const { data } = useSWR("/agent-mail/address", () => api.get("/agent-mail/address") as Promise<{ invite_address?: string }>);
  return data?.invite_address;
}

/** Onboarding shows until Go live: for a workspace with no document, or one that stopped part-way. */
export const needsOnboarding = (c: AgentConfig | undefined, isNew: boolean | undefined) =>
  !!c && (!!isNew || (!c.live && !!c.onboardingStep));

/** "+14842079294" as "(484) 207-9294"; anything else as it is. */
export const usPhone = (e164: string) => {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(e164);
  return m ? `(${m[1]}) ${m[2]}-${m[3]}` : e164;
};

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

/** Whole minutes of calls this month (the Home foot and the Billing card's fallback). */
export function monthMinutes(items: TimelineItem[], now = new Date()): number {
  const start = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
  return Math.round(items.filter((i): i is CallItem => i.type === "call" && i.at >= start)
    .reduce((s, c) => s + (c.durationSec ?? 0), 0) / 60);
}

export interface Usage {
  used: number; included: number; overageRate: number; runOut: "keep" | "forward";
  usedSeconds?: number; periodStart?: string; periodEnd?: string; timeZone?: string;
}

/** The server's measured allowance and billing period; unavailable usage must never become an estimated bill. */
export function useUsage() {
  const api = useApi();
  const { isLoaded, orgId, userId } = useAuth();
  return useSWR<Usage>(isLoaded && userId ? ["/voice/usage", orgId ?? userId, userId] : null,
    () => api.get("/voice/usage") as Promise<Usage>, { refreshInterval: 30_000 });
}

/** A factual recorded step can fill a missing call summary; guessed steps never do. */
export function callSummary(call: Pick<CallItem, "summary" | "steps">): string {
  return call.summary?.trim() || [...(call.steps ?? [])].reverse().find((step) => !step.guessed && step.text.trim())?.text.trim() || "Open conversation";
}
