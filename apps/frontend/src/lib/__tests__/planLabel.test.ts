import { describe, it, expect } from "vitest";
import { planLabel, isFreeTierExpired } from "../planLabel";

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
    expect(planLabel("trialing")).toBe("Trialing");
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
