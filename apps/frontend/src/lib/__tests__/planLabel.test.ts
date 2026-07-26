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

  it("passes other statuses through for the caller's capitalize treatment", () => {
    expect(planLabel("active")).toBe("active");
    expect(planLabel("trialing")).toBe("trialing");
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
