import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { automationRowLine, canDecide, deliveryRoomId, deliveryWords, nextRunWords, runNowReason, runsAgain, scheduleWords, waitingWords } from "./automations";

const sam = { userId: "sam", admin: false };
const admin = { userId: "ana", admin: true };

describe("who turns an automation on (routers/automations.py)", () => {
  it("a job the workspace owns (the builtin proactive jobs) is resumed by an org admin, never a member (_may_activate)", () => {
    const builtin = { deliverTo: "none", ownerUserId: "org_1", match: null };
    expect(canDecide(builtin, { ...admin, orgId: "org_1" })).toBe(true);
    expect(canDecide(builtin, { ...sam, orgId: "org_1" })).toBe(false);
    // Without the workspace id it stays the owner rule (nobody's user id is the org's).
    expect(canDecide(builtin, admin)).toBe(false);
    // A personal one is still its owner's.
    expect(canDecide({ deliverTo: "dm:sam", ownerUserId: "sam", match: null }, { ...sam, orgId: "org_1" })).toBe(true);
  });

  it("a rule goes by its match, not deliverTo", () => {
    const room = { deliverTo: "none", ownerUserId: "sam", match: { roomIds: ["r1"] } };
    expect(canDecide(room, sam)).toBe(false);
    expect(canDecide(room, admin)).toBe(true);
    expect(waitingWords(room)).toBe("Waiting on a workspace admin");
    const calendar = { deliverTo: "room:r1", ownerUserId: "sam", match: { sources: ["calendar"] } };
    expect(canDecide(calendar, sam)).toBe(true);
    expect(canDecide(calendar, admin)).toBe(false);
  });
  it("a schedule goes by where it posts", () => {
    expect(canDecide({ deliverTo: "room:r1", ownerUserId: "sam", match: null }, sam)).toBe(false);
    expect(canDecide({ deliverTo: "dm:sam", ownerUserId: "sam", match: null }, sam)).toBe(true);
  });
  it("Run now only while active", () => {
    expect(runNowReason({ status: "paused" }, true)).toBe("Resume it first");
    expect(runNowReason({ status: "paused_system" }, true)).toBe("Resume it first");
    expect(runNowReason({ status: "active" }, true)).toBeNull();
    expect(runNowReason({ status: "active" }, false)).toBe("Only its owner or an admin can run it");
  });
});

describe("automationRowLine (a Scheduled row)", () => {
  beforeEach(() => { vi.useFakeTimers({ now: Date.UTC(2026, 10, 5, 12), toFake: ["Date"] }); });
  afterEach(() => { vi.useRealTimers(); });
  const base = { pausedReason: null, match: null, schedule: { cron: ["0 9 * * 1"], tz: "UTC" }, nextRuns: ["2026-11-09T09:00:00Z"] };
  it("always a status, then the next run", () => {
    expect(automationRowLine({ ...base, status: "active" }, "UTC")).toBe("On · Mondays 9:00 AM UTC · next Mon 9 Nov, 9:00 AM");
    // A one-off reads as its date, never raw cron; Paused names no next run.
    const once = { ...base, schedule: { cron: ["27 10 5 11 *"], tz: "UTC" }, nextRuns: ["2026-11-05T10:27:00Z"] };
    expect(automationRowLine({ ...once, status: "active" }, "UTC")).toBe("On · Once, 5 Nov, 10:27 AM UTC");
    expect(automationRowLine({ ...once, status: "paused" }, "UTC")).toBe("Paused · Once, 5 Nov, 10:27 AM UTC");
    expect(automationRowLine({ ...base, status: "paused" }, "UTC")).toBe("Paused · Mondays 9:00 AM UTC");
    expect(automationRowLine({ ...base, status: "draft", nextRuns: [] }, "UTC")).toBe("Not turned on · Mondays 9:00 AM UTC");
    expect(automationRowLine({ ...base, status: "active", schedule: null, nextRuns: [], match: { sources: ["mail"] } }, "UTC")).toBe("On · runs on a match");
  });
});

describe("the confirm card's words", () => {
  const NOV = Date.UTC(2026, 10, 5, 12);
  it("a next run names its year only when it isn't this year", () => {
    expect(nextRunWords("2026-11-09T09:00:00Z", "UTC", NOV)).toBe("Mon 9 Nov, 9:00 AM");
    expect(nextRunWords("2027-01-04T09:00:00Z", "UTC", NOV)).toBe("Mon, 4 Jan 2027, 9:00 AM");
    expect(nextRunWords("not a date", "UTC", NOV)).toBe("");
  });
  it("a schedule it can't read is Custom schedule, with no zone echo", () => {
    expect(scheduleWords({ schedule: { cron: ["0 9 1 * *"], tz: "America/New_York" } }, "Europe/London", NOV)).toBe("Custom schedule");
  });
});

describe("W8: the time zone and the room are always named", () => {
  const NOV = Date.UTC(2026, 10, 5, 12);
  const ny = { schedule: { cron: ["0 9 * * 1"], tz: "America/New_York" } };
  it("the row's own zone, or the viewer's time with the row's zone beside it", () => {
    expect(scheduleWords(ny, "America/New_York", NOV)).toBe("Mondays 9:00 AM New York time");
    expect(scheduleWords(ny, "Europe/London", NOV)).toBe("Mondays 2:00 PM (New York time: Mondays 9:00 AM)");
  });
  it("posts to the room by name when it is known", () => {
    expect(deliveryWords("room:r8", "#walk-8")).toBe("posts to #walk-8");
    expect(deliveryWords("room:r8")).toBe("posts in a room");
    expect(deliveryWords("dm:sam", "#walk-8")).toBe("posts in a DM");
    expect(deliveryWords("none")).toBeNull();
    expect(deliveryRoomId("room:r8")).toBe("r8");
    expect(deliveryRoomId("dm:sam")).toBeNull();
  });
});

describe("runsAgain: Activity's Scheduled lists only what will run again (W13)", () => {
  const now = Date.UTC(2026, 10, 6, 12, 0); // 6 Nov 2026 12:00 UTC
  const once = (cron: string, extra: Record<string, unknown> = {}) =>
    ({ schedule: { cron: [cron], tz: "UTC", remainingRuns: null }, pausedReason: null, nextRuns: [], ...extra });

  it("a recurring schedule always does, paused or not; an event rule too", () => {
    expect(runsAgain(once("0 9 * * 1", { pausedReason: "paused by you" }), now)).toBe(true);
    expect(runsAgain({ schedule: null, pausedReason: null, nextRuns: [] }, now)).toBe(true);
  });

  it("a one-off the server turned off after its run does not", () => {
    expect(runsAgain(once("0 13 6 11 *", { pausedReason: "ran once" }), now)).toBe(false);
  });

  it("an old unlimited one-off already past does not, though Temporal lists next year's repeat", () => {
    expect(runsAgain(once("21 6 5 11 *", { nextRuns: ["2027-11-05T06:21:00+00:00"] }), now)).toBe(false);
    expect(runsAgain(once("0 13 6 11 *"), now)).toBe(true); // later today
  });

  it("a December ask for January is next year's, not hidden", () => {
    const dec = Date.UTC(2026, 11, 20, 12, 0);
    expect(runsAgain(once("0 9 5 1 *"), dec)).toBe(true);
  });

  it("a one-off limited to one run trusts the server's next fire time, whatever the year", () => {
    const july = once("0 9 1 7 *", { schedule: { cron: ["0 9 1 7 *"], tz: "UTC", remainingRuns: 1 }, nextRuns: ["2027-07-01T09:00:00+00:00"] });
    expect(runsAgain(july, now)).toBe(true);
  });
});
