import { describe, expect, it } from "vitest";
import {
  buildSections,
  countMatches,
  highlight,
  isNew,
  mailItemKey,
  meetingItemKey,
  meetingWhen,
  meetingEntryStatus,
  occurrenceOutcome,
  parseItemKey,
  parseTranscript,
  type MailMeeting,
  type MailMessage,
} from "../mailModel";

const NOW = new Date(2026, 9, 3, 15, 0).getTime(); // Sat Oct 3 2026, 15:00 local
const sec = (y: number, mo: number, d: number, h: number, mi = 0) => new Date(y, mo, d, h, mi).getTime() / 1000;

const message = (key: string, at: Date, direction: "in" | "out" = "in"): MailMessage => ({
  key, direction, from: "pat@acme.com", to: "agents@acme.co", subject: key, at: at.toISOString(),
  agent_id: null, text: "",
});

const meeting = (over: Partial<MailMeeting>): MailMeeting => ({
  id: "m1", title: "Sync", organizer: "pat@acme.com", status: "scheduled",
  start_at: sec(2026, 9, 4, 20), end_at: sec(2026, 9, 4, 20, 30), repeats: false, transcripts: [], ...over,
});

describe("buildSections", () => {
  it("orders Live now, Upcoming (soonest first), then day groups newest first", () => {
    const series = meeting({
      id: "series", title: "Weekly", repeats: true, status: "scheduled",
      start_at: sec(2026, 9, 10, 9), end_at: sec(2026, 9, 10, 9, 30),
      transcripts: [{ start_at: sec(2026, 9, 3, 9) }, { start_at: sec(2026, 8, 26, 9) }],
    });
    const live = meeting({ id: "live", title: "Live one", status: "in_call", start_at: sec(2026, 9, 3, 14) });
    const soon = meeting({ id: "soon", title: "Soon", start_at: sec(2026, 9, 4, 8), end_at: null });
    const cancelled = meeting({ id: "x", title: "Cancelled one-off", status: "cancelled", start_at: sec(2026, 9, 2, 11), end_at: sec(2026, 9, 2, 12) });
    const mail = [message("today", new Date(2026, 9, 3, 10)), message("old", new Date(2026, 8, 29, 10), "out")];

    const sections = buildSections([series, live, soon, cancelled], mail, "all", NOW);
    expect(sections.map((s) => s.title)).toEqual(["Live now", "Upcoming", "Today", "Yesterday", "Tue, Sep 29", "Sat, Sep 26"]);
    expect(sections[0].entries.map((e) => e.key)).toEqual([meetingItemKey("live", sec(2026, 9, 3, 14))]);
    expect(sections[1].entries.map((e) => e.key)).toEqual([
      meetingItemKey("soon", sec(2026, 9, 4, 8)),
      meetingItemKey("series", sec(2026, 9, 10, 9)),
    ]);
    // Today: the mail at 10:00 and the series' 09:00 occurrence, newest first.
    expect(sections[2].entries.map((e) => e.key)).toEqual([mailItemKey("today"), meetingItemKey("series", sec(2026, 9, 3, 9))]);
    const pastOccurrence = sections[2].entries[1];
    expect(pastOccurrence.kind === "meeting" && pastOccurrence.status).toBe("ended");
    expect(pastOccurrence.kind === "meeting" && pastOccurrence.hasTranscript).toBe(true);
    // The cancelled one-off sits at its end.
    expect(sections[3].entries[0].key).toBe(meetingItemKey("x", sec(2026, 9, 2, 11)));
    expect(sections.flatMap((s) => s.entries)).toHaveLength(8);
  });

  it("does not repeat a past one-off's own transcript as a second row", () => {
    const ended = meeting({ status: "ended", start_at: sec(2026, 9, 1, 9), transcripts: [{ start_at: sec(2026, 9, 1, 9) }] });
    const entries = buildSections([ended], [], "all", NOW).flatMap((s) => s.entries);
    expect(entries).toHaveLength(1);
    expect(entries[0].kind === "meeting" && entries[0].hasTranscript).toBe(true);
  });

  it("filters to mail or meetings", () => {
    const m = [meeting({})];
    const mail = [message("a", new Date(2026, 9, 3, 10))];
    expect(buildSections(m, mail, "mail", NOW).flatMap((s) => s.entries.map((e) => e.kind))).toEqual(["mail"]);
    expect(buildSections(m, mail, "meetings", NOW).flatMap((s) => s.entries.map((e) => e.kind))).toEqual(["meeting"]);
  });
});

describe("item keys", () => {
  it("round-trip", () => {
    expect(parseItemKey(mailItemKey("abc:def"))).toEqual({ kind: "mail", key: "abc:def" });
    expect(parseItemKey(meetingItemKey("11111111-1111-4111-8111-111111111111", 1790553600)))
      .toEqual({ kind: "meeting", id: "11111111-1111-4111-8111-111111111111", startAt: 1790553600 });
    expect(parseItemKey(meetingItemKey("m1", null))).toEqual({ kind: "meeting", id: "m1", startAt: null });
    expect(parseItemKey("nope")).toBeNull();
    expect(parseItemKey("meeting:m1:abc")).toBeNull();
  });
});

describe("meetingWhen", () => {
  it("says Today with a range, otherwise the date, and never Next", () => {
    const today = meetingWhen(sec(2026, 9, 3, 20), sec(2026, 9, 3, 20, 30), NOW);
    expect(today).toMatch(/^Today 8:00\s?–\s?8:30\sPM$/);
    expect(meetingWhen(sec(2026, 9, 4, 20), null, NOW)).toMatch(/^Sun, Oct 4, 8:00\sPM$/);
    // An occurrence that already started is described by its date, not as "Next".
    for (const startAt of [sec(2026, 9, 3, 9), sec(2026, 8, 1, 9), sec(2027, 0, 1, 9)]) {
      expect(meetingWhen(startAt, null, NOW)).not.toMatch(/Next/);
    }
  });
});

describe("parseTranscript", () => {
  it("merges a speaker's lines and keeps unparsed lines as plain text", () => {
    const start = Date.UTC(2026, 9, 3, 20, 0) / 1000;
    const parts = parseTranscript("[20:01] Pat: Hi all\n[20:01] Pat: Can you hear me?\n[20:02] Sam: Yes\nnoise line\n", start);
    expect(parts).toEqual([
      { kind: "turn", speaker: "Pat", at: Date.UTC(2026, 9, 3, 20, 1), text: "Hi all\nCan you hear me?" },
      { kind: "turn", speaker: "Sam", at: Date.UTC(2026, 9, 3, 20, 2), text: "Yes" },
      { kind: "plain", text: "noise line" },
    ]);
  });

  it("moves to the next UTC day when the call crosses midnight", () => {
    const start = Date.UTC(2026, 9, 3, 23, 50) / 1000;
    const parts = parseTranscript("[23:55] Pat: late\n[00:05] Sam: after midnight", start);
    expect(parts.map((p) => (p.kind === "turn" ? p.at : 0))).toEqual([
      Date.UTC(2026, 9, 3, 23, 55),
      Date.UTC(2026, 9, 4, 0, 5),
    ]);
  });
});

describe("find helpers", () => {
  it("highlights and counts case-insensitively", () => {
    expect(highlight("Budget and budget", "BUDGET")).toEqual([
      { text: "Budget", match: true }, { text: " and ", match: false }, { text: "budget", match: true },
    ]);
    expect(countMatches("aaa", "a")).toBe(3);
    expect(highlight("x", " ")).toEqual([{ text: "x", match: false }]);
  });
});

describe("isNew", () => {
  it("is received mail after the mark; sent mail and unavailable storage are never new", () => {
    const m = message("a", new Date(2026, 9, 3, 10));
    expect(isNew(m, 0)).toBe(true);
    expect(isNew(m, Date.parse(m.at))).toBe(false);
    expect(isNew(m, null)).toBe(false);
    expect(isNew({ ...m, direction: "out" }, 0)).toBe(false);
  });
});

describe("occurrenceOutcome", () => {
  const T = sec(2026, 9, 4, 20);
  const PAST = sec(2026, 8, 26, 9);
  const top = { ended_at: T + 60, notes: "posted" as const, unsaid: [{ agent_id: "a", agent_name: "A", request: "", answer: "x", at: T }] };

  it("is empty when the backend sends nothing", () => {
    expect(occurrenceOutcome(meeting({ transcripts: [{ start_at: T }] }), T)).toEqual({ ended_at: null, notes: null, unsaid: [] });
  });

  it("reads the top level for the current occurrence, its own entry for a past one", () => {
    const m = meeting({ ...top, transcripts: [{ start_at: T }, { start_at: PAST, ended_at: PAST + 5, notes: "skipped" }] });
    expect(occurrenceOutcome(m, T)).toEqual({ ended_at: T + 60, notes: "posted", unsaid: top.unsaid });
    expect(occurrenceOutcome(m, PAST)).toEqual({ ended_at: PAST + 5, notes: "skipped", unsaid: [] });
  });

  it("prefers the transcripts entry of the current occurrence", () => {
    const m = meeting({ ...top, transcripts: [{ start_at: T, notes: "pending" }] });
    expect(occurrenceOutcome(m, T).notes).toBe("pending");
  });
});


it("a recurring meeting's current lobby state never leaks into a historical transcript", () => {
  const m=meeting({status:"bot_created",start_at:NOW/1000+3600,end_at:NOW/1000+5400,last_bot_status:"in_waiting_room",transcripts:[{start_at:NOW/1000-86400}]});
  const entries=buildSections([m],[],"all",NOW).flatMap(s=>s.entries).flatMap(e=>e.kind==="meeting"?[e]:[]);
  const current=entries.find(e=>e.startAt===m.start_at)!;
  const past=entries.find(e=>e.startAt!==m.start_at)!;
  expect(meetingEntryStatus(current,NOW).label).toBe("Waiting for host");
  expect(meetingEntryStatus(past,NOW).label).toBe("Ended");
  expect(meetingEntryStatus(past,NOW).joined).toBe(true);
});
