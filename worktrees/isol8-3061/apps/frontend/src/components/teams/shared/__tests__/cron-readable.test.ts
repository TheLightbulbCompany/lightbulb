import { describe, it, expect } from "vitest";
import { buildCron, describeCron, parseCron, type Schedule } from "../cron-readable";

describe("describeCron", () => {
  it("daily", () => expect(describeCron("0 8 * * *")).toBe("Every day at 08:00"));
  it("weekday", () => expect(describeCron("0 8 * * 1")).toBe("Every Monday at 08:00"));
  it("hourly", () => expect(describeCron("0 * * * *")).toBe("Every hour"));
  it("weekdays", () =>
    expect(describeCron("0 7 * * 1-5")).toBe("Every weekday at 07:00"));
  it("unparseable falls back", () =>
    expect(describeCron("*/13 3 5 6 2")).toBe("*/13 3 5 6 2"));
  it("out-of-range minute falls back", () =>
    expect(describeCron("75 8 * * *")).toBe("75 8 * * *"));
  it("out-of-range hour falls back", () =>
    expect(describeCron("0 25 * * *")).toBe("0 25 * * *"));
  it("hourly at a fixed minute", () =>
    expect(describeCron("15 * * * *")).toBe("Every hour at :15"));
});

describe("parseCron / buildCron", () => {
  // Round-tripping is the contract the schedule picker rests on: whatever it
  // builds must read back as friendly text in the automations list, and
  // whatever the list shows as friendly text must reopen in the picker.
  const cases: Array<[string, Schedule]> = [
    ["0 * * * *", { freq: "hourly", hour: 0, minute: 0, day: 1 }],
    ["30 * * * *", { freq: "hourly", hour: 0, minute: 30, day: 1 }],
    ["0 9 * * *", { freq: "daily", hour: 9, minute: 0, day: 1 }],
    ["45 17 * * *", { freq: "daily", hour: 17, minute: 45, day: 1 }],
    ["0 7 * * 1-5", { freq: "weekdays", hour: 7, minute: 0, day: 1 }],
    ["0 8 * * 1", { freq: "weekly", hour: 8, minute: 0, day: 1 }],
    ["0 8 * * 0", { freq: "weekly", hour: 8, minute: 0, day: 0 }],
  ];

  it.each(cases)("%s round-trips through the picker", (expr, schedule) => {
    expect(parseCron(expr)).toEqual(schedule);
    expect(buildCron(schedule)).toBe(expr);
    // Anything the picker can hold must also render as a sentence, never raw.
    expect(describeCron(expr)).not.toBe(expr);
  });

  it.each([
    ["*/13 3 5 6 2"], // steps + day-of-month + month
    ["0 9 1 * *"], // day-of-month
    ["0 9,17 * * *"], // hour list
    ["0 9 * * 2-4"], // day range the picker can't name
    ["75 8 * * *"], // out of range
    ["0 9 * *"], // malformed
  ])("%s is outside the picker's vocabulary", (expr) => {
    expect(parseCron(expr)).toBeNull();
  });
});
