import { describe, it, expect } from "vitest";
import { planLabel, isFreeTierExpired, freeTierDaysLeft, FREE_TIER_DAYS } from "../planLabel";

describe("planLabel", () => {
  it("never leaks the raw trial_local enum", () => {
    // CSS uppercases the plan badge, so an unmapped enum rendered as
    // "TRIAL_LOCAL" in the sidebar — developer-facing text in a user surface.
    expect(planLabel("trial_local")).toBe("Free tier");
  });

  it("distinguishes an elapsed free-tier window", () => {
    expect(planLabel("trial_local", { expired: true })).toBe("Free tier ended");
  });

  it("capitalizes single-word statuses for the caller's own treatment on top", () => {
    expect(planLabel("active")).toBe("Active");
    expect(planLabel("trialing")).toBe("Free trial");
  });

  it("humanizes underscored multi-word statuses instead of leaking the raw enum", () => {
    // CSS `capitalize`/`uppercase` can't fix an underscore — "past_due" under
    // `capitalize` reads "Past_due", not "Past due". planLabel does the word
    // split itself so every caller gets a readable label regardless of its
    // own textTransform.
    expect(planLabel("past_due")).toBe("Past due");
    expect(planLabel("incomplete_expired")).toBe("Incomplete expired");
  });

  it("returns null for an absent status so callers can skip the chip", () => {
    expect(planLabel(null)).toBeNull();
  });
});

describe("isFreeTierExpired", () => {
  // Fixed "now" so past/future fixtures below are unambiguous regardless of
  // when the suite runs.
  const nowMs = 1_800_000_000_000;

  it("is true once trial_end is in the past for a trial_local owner", () => {
    const pastSec = nowMs / 1000 - 86_400;
    expect(isFreeTierExpired("trial_local", pastSec, nowMs)).toBe(true);
  });

  it("is false while trial_end is still in the future", () => {
    const futureSec = nowMs / 1000 + 86_400;
    expect(isFreeTierExpired("trial_local", futureSec, nowMs)).toBe(false);
  });

  it("is false for a non-trial_local status even with a past trial_end", () => {
    // Only trial_local rows carry the free-tier window semantics; a stale
    // trial_end on any other status must not be treated as an expiry.
    const pastSec = nowMs / 1000 - 86_400;
    expect(isFreeTierExpired("active", pastSec, nowMs)).toBe(false);
  });

  it("is false when trial_end is null or undefined", () => {
    expect(isFreeTierExpired("trial_local", null, nowMs)).toBe(false);
    expect(isFreeTierExpired("trial_local", undefined, nowMs)).toBe(false);
  });
});

const DAY = 86_400_000;

describe("freeTierDaysLeft", () => {
  it("rounds up, so a partly-spent day still reads as a day left", () => {
    // The badge is a nudge. Telling someone with six hours left that they have
    // none is the one direction worth avoiding.
    const now = Date.UTC(2026, 0, 1, 0, 0, 0);
    expect(freeTierDaysLeft("trial_local", (now + 6 * 3_600_000) / 1000, now)).toBe(1);
    expect(freeTierDaysLeft("trial_local", (now + 2 * DAY) / 1000, now)).toBe(2);
  });

  it("floors at 0 once the window has elapsed", () => {
    const now = Date.UTC(2026, 0, 5);
    expect(freeTierDaysLeft("trial_local", (now - 3 * DAY) / 1000, now)).toBe(0);
  });

  it("clamps to the trial length, so a fresh signup never reads 31 on a 30-day trial", () => {
    // trial_end is stamped signup + FREE_TIER_DAYS exactly, but nowMs is the
    // CLIENT clock. A machine 5 min slow makes trial_end - now exceed the window
    // and the ceil round to 4 — the clamp must cap it at the promised length.
    const now = Date.UTC(2026, 0, 1);
    const trialEnd = (now + FREE_TIER_DAYS * DAY) / 1000;
    expect(freeTierDaysLeft("trial_local", trialEnd, now)).toBe(FREE_TIER_DAYS);
    const skewedNow = now - 5 * 60_000; // client clock 5 minutes behind
    expect(freeTierDaysLeft("trial_local", trialEnd, skewedNow)).toBe(FREE_TIER_DAYS);
  });

  it("is null when there is no window to describe", () => {
    const now = Date.UTC(2026, 0, 1);
    expect(freeTierDaysLeft("active", (now + DAY) / 1000, now)).toBeNull();
    // Legacy dev rows carry no trial_end, and the backend reads a missing
    // stamp as NOT expired — so we must not invent a countdown for them.
    expect(freeTierDaysLeft("trial_local", null, now)).toBeNull();
    expect(freeTierDaysLeft("trial_local", undefined, now)).toBeNull();
  });
});
