import { meetingStatus } from "@/lib/meetingStatus";

// Pure model for the Mail panel: the wire types, the list's sections, item keys,
// transcript parsing and time formatting. No React, no I/O.

export interface MailMessage {
  key: string;
  direction: "in" | "out";
  from: string;
  to: string;
  subject: string;
  at: string;
  agent_id: string | null;
  text: string;
  truncated?: boolean;
  channel?: "mail" | "text";
}

export type MeetingNotes = "posted" | "skipped" | "pending";

export interface UnsaidAnswer {
  agent_id: string;
  agent_name: string;
  request: string;
  answer: string;
  at: number;
}

/** What one occurrence ended with. Every field is absent from an older backend. */
export interface MeetingOutcome {
  ended_at?: number | null;
  notes?: MeetingNotes | null;
  unsaid?: UnsaidAnswer[];
}

export interface MailMeeting extends MeetingOutcome {
  id: string;
  title: string;
  organizer: string | null;
  invited_by?: string | null;
  status: string | null;
  last_bot_status?: string | null;
  in_call_at?: number | null;
  start_at: number | null;
  end_at: number | null;
  repeats: boolean;
  transcripts: ({ start_at: number } & MeetingOutcome)[];
  notes_room_id?: string | null;
  reason?: string | null;
}

export interface MailPage {
  address: string;
  meetings?: MailMeeting[];
  messages?: MailMessage[];
  next?: string | null;
}

export type MailFilter = "all" | "mail" | "meetings";
export const MAIL_FILTERS: { id: MailFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "mail", label: "Mail" },
  { id: "meetings", label: "Meetings" },
];

export type MailEntry =
  | { kind: "mail"; key: string; at: number; message: MailMessage }
  | {
      kind: "meeting";
      key: string;
      at: number;
      meeting: MailMeeting;
      /** The occurrence this row stands for (a series has one row per past occurrence). */
      startAt: number | null;
      endAt: number | null;
      status: string | null;
      hasTranscript: boolean;
    };

export interface MailSection {
  key: string;
  title: string;
  entries: MailEntry[];
}

// ---- item keys -------------------------------------------------------------

export function mailItemKey(messageKey: string): string {
  return `mail:${messageKey}`;
}

export function meetingItemKey(meetingId: string, startAt: number | null): string {
  return `meeting:${meetingId}:${startAt ?? ""}`;
}

export type ParsedItemKey =
  | { kind: "mail"; key: string }
  | { kind: "meeting"; id: string; startAt: number | null };

export function parseItemKey(item: string | null | undefined): ParsedItemKey | null {
  if (!item) return null;
  if (item.startsWith("mail:")) return item.length > 5 ? { kind: "mail", key: item.slice(5) } : null;
  if (!item.startsWith("meeting:")) return null;
  const rest = item.slice(8);
  const cut = rest.lastIndexOf(":");
  if (cut <= 0) return null;
  const raw = rest.slice(cut + 1);
  const startAt = raw === "" ? null : Number(raw);
  if (startAt !== null && !Number.isFinite(startAt)) return null;
  return { kind: "meeting", id: rest.slice(0, cut), startAt };
}

// ---- statuses --------------------------------------------------------------

const LIVE = new Set(["in_call"]);
export const UPCOMING: ReadonlySet<string> = new Set(["scheduled", "bot_created"]);
const PAST = new Set(["ended", "cancelled", "bot_failed"]);

/** Adapt the current occurrence only: a series' next bot is not its past transcript. */
export function meetingEntryStatus(entry: MailEntry & { kind: "meeting" }, now: number) {
  const current = entry.startAt === entry.meeting.start_at;
  return meetingStatus({ status: entry.status, startAt: entry.startAt, hasTranscript: entry.hasTranscript,
    lastBotStatus: current ? entry.meeting.last_bot_status : null,
    inCallAt: current ? entry.meeting.in_call_at : null }, now);
}

// ---- sections --------------------------------------------------------------

function messageEntry(message: MailMessage): MailEntry {
  const at = Date.parse(message.at);
  return { kind: "mail", key: mailItemKey(message.key), at: Number.isNaN(at) ? 0 : at, message };
}

function occurrence(meeting: MailMeeting, startAt: number | null, status: string | null): MailEntry & { kind: "meeting" } {
  const current = startAt === meeting.start_at;
  // A past occurrence of a series keeps the series' length.
  const length = meeting.start_at !== null && meeting.end_at !== null ? meeting.end_at - meeting.start_at : null;
  const endAt = current ? meeting.end_at : startAt !== null && length !== null ? startAt + length : null;
  const at = ((current ? meeting.end_at ?? meeting.start_at : startAt) ?? 0) * 1000;
  return {
    kind: "meeting",
    key: meetingItemKey(meeting.id, startAt),
    at,
    meeting,
    startAt,
    endAt,
    status,
    hasTranscript: startAt !== null && meeting.transcripts.some((t) => t.start_at === startAt),
  };
}

/**
 * The outcome of the occurrence at `startAt`: its transcripts entry's value when that entry
 * carries one, else the meeting's own when `startAt` is the current occurrence.
 */
export function occurrenceOutcome(meeting: MailMeeting, startAt: number | null): Required<MeetingOutcome> {
  const entry = startAt === null ? undefined : meeting.transcripts.find((t) => t.start_at === startAt);
  const current = startAt === meeting.start_at ? meeting : undefined;
  return {
    ended_at: entry?.ended_at ?? current?.ended_at ?? null,
    notes: entry?.notes ?? current?.notes ?? null,
    unsaid: entry?.unsaid ?? current?.unsaid ?? [],
  };
}

const DAY_HEADING = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });

function dayStart(ms: number): number {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

export function dayTitle(ms: number, now: number): string {
  const day = dayStart(ms);
  const today = dayStart(now);
  if (day === today) return "Today";
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  if (day === yesterday.getTime()) return "Yesterday";
  return DAY_HEADING.format(new Date(ms));
}

/**
 * Live now, Upcoming (soonest first), then day groups newest first, where mail and
 * past meetings mix. A meeting's current occurrence goes where its status says; every
 * other transcript of it is a past occurrence row.
 */
export function buildSections(
  meetings: readonly MailMeeting[],
  messages: readonly MailMessage[],
  filter: MailFilter,
  now: number,
): MailSection[] {
  const live: MailEntry[] = [];
  const upcoming: (MailEntry & { kind: "meeting" })[] = [];
  const past: MailEntry[] = filter === "meetings" ? [] : messages.map(messageEntry);
  if (filter !== "mail") {
    for (const meeting of meetings) {
      const status = meeting.status;
      const current = occurrence(meeting, meeting.start_at, status);
      if (LIVE.has(status ?? "")) live.push(current);
      else if (UPCOMING.has(status ?? "")) upcoming.push(current);
      else if (PAST.has(status ?? "")) past.push(current);
      // An unknown status goes by the clock.
      else if ((meeting.start_at ?? 0) * 1000 > now) upcoming.push(current);
      else past.push(current);
      for (const t of meeting.transcripts) {
        if (t.start_at !== meeting.start_at) past.push(occurrence(meeting, t.start_at, "ended"));
      }
    }
  }
  upcoming.sort((a, b) => (a.startAt ?? 0) - (b.startAt ?? 0));
  past.sort((a, b) => b.at - a.at);

  const sections: MailSection[] = [];
  if (live.length) sections.push({ key: "live", title: "Live now", entries: live });
  if (upcoming.length) sections.push({ key: "upcoming", title: "Upcoming", entries: upcoming });
  for (const entry of past) {
    const key = `day-${dayStart(entry.at)}`;
    const last = sections[sections.length - 1];
    if (last?.key === key) last.entries.push(entry);
    else sections.push({ key, title: dayTitle(entry.at, now), entries: [entry] });
  }
  return sections;
}

// ---- time ------------------------------------------------------------------

const TIME = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});
const FULL = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "short" });
const SHORT_DAY = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });

export function clockTime(ms: number): string {
  return TIME.format(new Date(ms));
}

export function fullDateTime(ms: number): string {
  return FULL.format(new Date(ms));
}

export function shortDay(seconds: number): string {
  return SHORT_DAY.format(new Date(seconds * 1000));
}

/** "Today 8:00 – 8:30 PM" for today, else "Sun, Oct 4, 8:00 PM". Never "Next …". */
export function meetingWhen(startAt: number | null, endAt: number | null, now: number): string {
  if (startAt === null) return "";
  const start = new Date(startAt * 1000);
  if (dayStart(start.getTime()) !== dayStart(now)) return DATE_TIME.format(start);
  if (endAt === null || endAt <= startAt) return `Today ${TIME.format(start)}`;
  return `Today ${TIME.formatRange(start, new Date(endAt * 1000))}`;
}

// ---- transcript ------------------------------------------------------------

export type TranscriptPart =
  | { kind: "turn"; speaker: string; at: number; text: string }
  | { kind: "plain"; text: string };

const LINE = /^\[(\d{1,2}):(\d{2})\]\s+([^:]+?):\s?(.*)$/;
const DAY_MS = 86_400_000;

/**
 * The server writes `[HH:MM] Name: text` with HH:MM in UTC. Each line's absolute time is
 * the occurrence's UTC day plus HH:MM, moved a day on when it would fall well before the
 * line before it (a call across UTC midnight). Same-speaker runs merge into one turn.
 */
export function parseTranscript(raw: string, occurrenceStart: number): TranscriptPart[] {
  const start = occurrenceStart * 1000;
  const base = new Date(start);
  base.setUTCHours(0, 0, 0, 0);
  let day = base.getTime();
  let ref = start;
  const parts: TranscriptPart[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const m = LINE.exec(line);
    const hours = m ? Number(m[1]) : NaN;
    const minutes = m ? Number(m[2]) : NaN;
    if (!m || hours > 23 || minutes > 59) {
      parts.push({ kind: "plain", text: line });
      continue;
    }
    let at = day + (hours * 60 + minutes) * 60_000;
    // ponytail: 6 h slack; a call that skips 18 h of silence would misdate.
    while (at < ref - 6 * 3_600_000) {
      day += DAY_MS;
      at += DAY_MS;
    }
    ref = at;
    const speaker = m[3].trim();
    const prev = parts[parts.length - 1];
    if (prev?.kind === "turn" && prev.speaker === speaker) prev.text += `\n${m[4]}`;
    else parts.push({ kind: "turn", speaker, at, text: m[4] });
  }
  return parts;
}

export type Highlighted = { text: string; match: boolean }[];

/** Splits `text` around case-insensitive occurrences of `query`. */
export function highlight(text: string, query: string): Highlighted {
  const q = query.trim().toLowerCase();
  if (!q) return [{ text, match: false }];
  const out: Highlighted = [];
  const lower = text.toLowerCase();
  let from = 0;
  for (let at = lower.indexOf(q); at !== -1; at = lower.indexOf(q, from)) {
    if (at > from) out.push({ text: text.slice(from, at), match: false });
    out.push({ text: text.slice(at, at + q.length), match: true });
    from = at + q.length;
  }
  if (from < text.length) out.push({ text: text.slice(from), match: false });
  return out;
}

export function countMatches(text: string, query: string): number {
  return highlight(text, query).filter((part) => part.match).length;
}

// ---- new -------------------------------------------------------------------

/** Received mail newer than `seen` (epoch ms). `seen === null` means storage is unavailable: nothing is new. */
export function isNew(message: MailMessage, seen: number | null): boolean {
  if (seen === null || message.direction !== "in") return false;
  const at = Date.parse(message.at);
  return !Number.isNaN(at) && at > seen;
}

export function newestReceived(messages: readonly MailMessage[]): number | null {
  let newest: number | null = null;
  for (const m of messages) {
    if (m.direction !== "in") continue;
    const at = Date.parse(m.at);
    if (!Number.isNaN(at) && (newest === null || at > newest)) newest = at;
  }
  return newest;
}
