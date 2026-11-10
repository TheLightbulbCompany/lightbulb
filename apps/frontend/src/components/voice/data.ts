"use client";

// The owner's voice surfaces read three things: the timeline (calls + meetings), the assistant's settings
// (one JSON document, GET/PUT /voice/agent) and the open Questions (/needs-you). Shapes: routers/voice.py.

import { useCallback } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { usPhone, type QuestionView } from "@lightbulb/shared";
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
  title?: string; startAt?: number; endAt?: number; status?: string; inCallAt?: number; endedAt?: number; lastBotStatus?: string; people?: string[]; summary?: string; notes?: string;
  asked?: { t?: number; who?: string; q: string; a?: string }[];
  followUps?: { id?: string; text: string; status?: string; taskId?: string }[];
  /** From the notes run's "Decisions:" section; `by` only when someone said who decided. */
  decisions?: { text: string; by?: string }[];
  /** Set on a recurring series' held occurrence (its own row, from the meeting's history): that `startAt`. */
  occurrence?: number; hasTranscript?: boolean;
}

export type TimelineItem = CallItem | MeetingItem;

/** One row per occurrence: a series' past occurrences share its room. */
export const rowId = (i: TimelineItem) => i.type === "meeting" && i.occurrence != null ? `${i.roomId}@${i.occurrence}` : i.roomId;

export function useTimeline() {
  const api = useApi();
  // ponytail: a 20 s poll, not a push; subscribe to the room events when calls must land live.
  return useSWR("/voice/timeline", async () => ((await api.get("/voice/timeline")) as { items: TimelineItem[] }).items,
    { refreshInterval: 20_000 });
}

export interface Knowledge { q: string; a: string; source?: string }
export interface StaffMember { id: string; name: string }
export type Day = { open: string; close: string; closed?: boolean };

export interface AgentConfig {
  /** Existing worker UUID that greets people; null uses the stable workspace default. */
  frontAgentId?: string | null;
  name: string; greeting: string; voice: string; spanish: boolean; live: boolean; paused: boolean;
  allow: { book: boolean; move: boolean; waitlist?: boolean; greetByName: boolean; answer: boolean };
  knowledge: Knowledge[]; toAdd: string[]; instructions: string;
  hours: Record<string, Day>; holidays: { from: string; to: string }[];
  /** `minutesGuessed`: the onboarding draft guessed the duration; cleared when the owner sets it. */
  services: { name: string; minutes: number; price: string; staff: string; minutesGuessed?: boolean }[]; bufferMin: number;
  /** The people who take bookings: one booking per free person at a time. `id` is set when the person is added and
   *  never changes (their bookings are keyed by it), so renaming, removing or reordering moves nobody's bookings.
   *  A service's `staff` is the comma-separated ids of who does it ("" = anyone). Empty list: one at a time. */
  staff: StaffMember[];
  /** IANA zone the hours are in; Onboarding saves the browser's. The server defaults to America/New_York. */
  timezone?: string;
  bookingsGo: "google" | "square" | "other" | "own"; otherSystem: string;
  number: string; forwardFrom: string;
  /** Set by the server when the forwarding check's call came back to the line (POST /voice/forward-check). */
  forwardingOk?: boolean;
  /** Onboarding step 4, stored by POST /voice/number; Go live reads it. */
  numberChoice?: { path: "keep" | "new"; areaCode?: string | null; carrier?: string | null; mode?: "missed" | "all" | null };
  handoff: { number: string; onlyOpenHours: boolean };
  meetings: { followUps: boolean };
  reminders: { dayBefore: boolean; missedCallers: boolean; waitlist: boolean };
  /** Past the included minutes: keep answering at the overage rate, or send calls to the owner's phone. */
  runOut?: "keep" | "forward";
  /** Onboarding: what the owner typed in step 1, and the step to resume at (absent once it went live). */
  business?: string; city?: string; website?: string; onboardingStep?: number;
}

export const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

export const DEFAULT_CONFIG: AgentConfig = {
  name: "", greeting: "", voice: "marin", spanish: false, live: false, paused: false,
  allow: { book: true, move: true, waitlist: true, greetByName: true, answer: true },
  knowledge: [], toAdd: [], instructions: "",
  hours: Object.fromEntries(DAYS.map((d) => [d, { open: "09:00", close: "18:00", closed: d === "Sun" }])),
  holidays: [],
  services: [], bufferMin: 10, staff: [],
  bookingsGo: "own", otherSystem: "",
  number: "", forwardFrom: "",
  handoff: { number: "", onlyOpenHours: true },
  meetings: { followUps: true },
  reminders: { dayBefore: false, missedCallers: false, waitlist: false },
};

/** In the app the assistant is always Lightbulb (founder, 8 Nov 2026); the Agent-page name is only what callers hear. */
export const LIGHTBULB = "Lightbulb";

/** The name callers hear; "Lightbulb" until the owner picks one. */
export const nameOf = (c?: Partial<AgentConfig> | null) => c?.name?.trim() || LIGHTBULB;

/** The Agent page document, defaults filled in. `isNew`: the workspace has no document yet (first run). */
export function useAgentConfig() {
  const api = useApi();
  const swr = useSWR("/voice/agent", async () => {
    const { config, agents = [], frontAgentId, frontAgentIssue } = (await api.get("/voice/agent")) as {
      config: Partial<AgentConfig> | null; agents?: { id: string; name: string }[]; frontAgentId?: string | null; frontAgentIssue?: string | null;
    };
    // The picker is gone, so a saved selection that no longer exists is dropped here or no edit could save.
    const dead = !!config?.frontAgentId && !agents.some((a) => a.id === config.frontAgentId);
    return { config: { ...DEFAULT_CONFIG, ...config, ...(dead && { frontAgentId: null }) } as AgentConfig, isNew: config == null, agents, frontAgentId, frontAgentIssue };
  });
  const { mutate } = swr;
  const save = useCallback(async (next: AgentConfig) => {
    await api.put("/voice/agent", { config: next });
    await mutate((current) => ({ config: next, isNew: false, agents: current?.agents ?? [],
      frontAgentId: next.frontAgentId ?? current?.frontAgentId, frontAgentIssue: undefined }), { revalidate: true });
  }, [api, mutate]);
  return { agents: swr.data?.agents ?? [], frontAgentId: swr.data?.frontAgentId, frontAgentIssue: swr.data?.frontAgentIssue, data: swr.data?.config, isNew: swr.data?.isNew, error: swr.error, isLoading: swr.isLoading, save };
}

export interface WaitlistEntry { id: string; date: string; name: string; phone: string; service: string; note: string }

/** Callers waiting for a day (GET /voice/waitlist); `remove` deletes one entry. */
export function useWaitlist() {
  const api = useApi();
  const swr = useSWR("/voice/waitlist", async () => ((await api.get("/voice/waitlist")) as { entries?: WaitlistEntry[] } | undefined)?.entries ?? [], { refreshInterval: 30000 }); // focus revalidation is off app-wide: poll so new callers appear
  const { mutate } = swr;
  const remove = useCallback(async (e: WaitlistEntry) => {
    await api.del(`/voice/waitlist/${e.date}/${e.id}`);
    await mutate();
  }, [api, mutate]);
  return { entries: swr.data ?? [], remove };
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

/** Re-exported from @lightbulb/shared (web and phone share one formatter). */
export { usPhone };

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
export const who = (c: CallItem) => c.callerName || (c.caller ? usPhone(c.caller) : "Unknown caller");

/** Whole minutes of calls this month (the Home foot and the Billing card's fallback). */
export function monthMinutes(items: TimelineItem[], now = new Date()): number {
  const start = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
  return Math.round(items.filter((i): i is CallItem => i.type === "call" && i.at >= start)
    .reduce((s, c) => s + (c.durationSec ?? 0), 0) / 60);
}

export interface Usage {
  used: number; included: number; overageRate: number; runOut: "keep" | "forward";
  usedSeconds?: number; periodStart?: string; periodEnd?: string; timeZone?: string;
  paidAllowance?: boolean;
}

/** The server's measured allowance and billing period; unavailable usage must never become an estimated bill. */
export function useUsage() {
  const api = useApi();
  const { isLoaded, orgId, userId } = useAuth();
  return useSWR<Usage>(isLoaded && userId ? ["/voice/usage", orgId ?? userId, userId] : null,
    async () => { const u = (await api.get("/voice/usage")) as Usage; return { ...u, used: Math.ceil(u.used) }; }, // whole minutes: the server's 9.283 shows as 10
    { refreshInterval: 30_000 });
}

/** A factual recorded step can fill a missing call summary; guessed steps never do. */
export function callSummary(call: Pick<CallItem, "summary" | "steps">): string {
  return call.summary?.trim() || [...(call.steps ?? [])].reverse().find((step) => !step.guessed && step.text.trim())?.text.trim() || "Open conversation";
}
