import { describe, it, expect } from "vitest";
import { planLabel } from "../planLabel";

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
