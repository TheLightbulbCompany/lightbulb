import { describe, expect, it, vi } from "vitest";
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({}) }));
import { applyDraft, withTimezone } from "../Onboarding";
import { DAYS, DEFAULT_CONFIG, needsOnboarding, usPhone } from "../data";

const week = Object.fromEntries(DAYS.map((d) => [d, { open: "10:00", close: "19:00", closed: false }]));

describe("voice onboarding", () => {
  it("opens for a new workspace or one that stopped part-way, never after Go live", () => {
    expect(needsOnboarding(DEFAULT_CONFIG, true)).toBe(true);
    expect(needsOnboarding({ ...DEFAULT_CONFIG, onboardingStep: 3 }, false)).toBe(true);
    expect(needsOnboarding({ ...DEFAULT_CONFIG, onboardingStep: 3, live: true }, false)).toBe(false);
    expect(needsOnboarding(DEFAULT_CONFIG, false)).toBe(false);
    expect(needsOnboarding(undefined, true)).toBe(false);
  });
  it("folds the site draft in: a full week of hours, services, answers, staff, and the listed phone", () => {
    const c = applyDraft(DEFAULT_CONFIG, {
      read: true, hours: week, staff: ["Ana", "Mei"], phone: "(415) 555-0199", toAdd: ["Parking?"],
      services: [{ name: "Gel", minutes: 45, price: "$55", staff: "", minutesGuessed: true }],
      knowledge: [{ q: "Walk-ins?", a: "Yes", source: "from site" }],
    });
    expect(c.hours.Mon.open).toBe("10:00");
    expect(c.services[0].minutesGuessed).toBe(true);
    expect(c.knowledge.map((k) => k.q)).toEqual(["Walk-ins?", "Who works here?"]);
    expect(c.toAdd).toEqual(["Parking?"]);
    expect(c.forwardFrom).toBe("(415) 555-0199");
  });
  it("keeps the defaults when the site said nothing", () => {
    const c = applyDraft(DEFAULT_CONFIG, { read: false, hours: {}, services: [], staff: [], knowledge: [], toAdd: [], phone: "" });
    expect(c.hours).toEqual(DEFAULT_CONFIG.hours);
    expect(c.knowledge).toEqual([]);
  });
  it("saves the browser's time zone unless one is already set", () => {
    expect(withTimezone(DEFAULT_CONFIG).timezone).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
    expect(withTimezone({ ...DEFAULT_CONFIG, timezone: "America/Los_Angeles" }).timezone).toBe("America/Los_Angeles");
  });
  it("formats a US number", () => {
    expect(usPhone("+14842079294")).toBe("(484) 207-9294");
    expect(usPhone("+442071234567")).toBe("+442071234567");
  });
});

describe("a second read of the site", () => {
  it("fills only what is still empty and never overwrites the owner's edits", () => {
    const owner = {
      ...DEFAULT_CONFIG, hours: { ...DEFAULT_CONFIG.hours, Mon: { open: "08:00", close: "12:00" } },
      services: [{ name: "Mine", minutes: 30, price: "$1", staff: "" }],
      knowledge: [{ q: "Do you take walk-ins?", a: "No" }], forwardFrom: "(512) 555-0100",
    };
    const c = applyDraft(owner, {
      read: true, hours: week, staff: [], phone: "(415) 555-0199", toAdd: ["Do you take walk-ins?", "Parking?"],
      services: [{ name: "Gel", minutes: 45, price: "$55", staff: "" }],
      knowledge: [{ q: "do you take walk-ins?", a: "Yes", source: "from site" }, { q: "Gift cards?", a: "Yes", source: "from site" }],
    });
    expect(c.hours.Mon.open).toBe("08:00");
    expect(c.services.map((s) => s.name)).toEqual(["Mine"]);
    expect(c.knowledge.map((k) => [k.q, k.a])).toEqual([["Do you take walk-ins?", "No"], ["Gift cards?", "Yes"]]);
    expect(c.toAdd).toEqual(["Parking?"]);
    expect(c.forwardFrom).toBe("(512) 555-0100");
  });
  it("keeps the consent-gated texts off by default", () => {
    expect(DEFAULT_CONFIG.reminders.missedCallers).toBe(false);
    expect(DEFAULT_CONFIG.reminders.waitlist).toBe(false);
  });
});
