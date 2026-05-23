import { describe, expect, it } from "vitest";
import { nextOnboardingCompletion } from "@/components/chat/onboardingAnalytics";

type Phase = "payment" | "container" | "gateway" | "channels" | "ready";

const STEPS_PAID = [
  { phase: "payment" as Phase },
  { phase: "container" as Phase },
  { phase: "gateway" as Phase },
  { phase: "ready" as Phase },
];

const STEPS_FREE = [
  { phase: "container" as Phase },
  { phase: "gateway" as Phase },
  { phase: "ready" as Phase },
];

describe("nextOnboardingCompletion", () => {
  it("emits on forward in-list transition (paid: payment → container)", () => {
    expect(nextOnboardingCompletion("payment", "container", STEPS_PAID)).toEqual({
      step_name: "payment",
      step_index: 0,
    });
  });

  it("emits gateway completion when transitioning into channels (off-list curr)", () => {
    // channels is not in STEPS_PAID; the gateway → channels transition
    // must still record gateway as completed, otherwise paid users with
    // channel setup get under-counted.
    expect(nextOnboardingCompletion("gateway", "channels", STEPS_PAID)).toEqual({
      step_name: "gateway",
      step_index: 2,
    });
  });

  it("does NOT emit on backward transition (channels → payment)", () => {
    // Channels is not in the list, so prevIdx is -1 → return null.
    expect(nextOnboardingCompletion("channels", "payment", STEPS_PAID)).toBeNull();
  });

  it("does NOT emit on backward in-list transition (gateway → container)", () => {
    expect(nextOnboardingCompletion("gateway", "container", STEPS_PAID)).toBeNull();
  });

  it("does NOT emit when current equals prev (defensive — caller should also gate)", () => {
    expect(nextOnboardingCompletion("gateway", "gateway", STEPS_PAID)).toBeNull();
  });

  it("works for free tier (no payment step)", () => {
    expect(nextOnboardingCompletion("container", "gateway", STEPS_FREE)).toEqual({
      step_name: "container",
      step_index: 0,
    });
    expect(nextOnboardingCompletion("gateway", "ready", STEPS_FREE)).toEqual({
      step_name: "gateway",
      step_index: 1,
    });
  });

  it("emits when leaving the list past the last step (ready → off-list, hypothetical)", () => {
    // Defensive: if a future Phase value isn't yet added to the list,
    // we still emit completion of the previous in-list step rather than
    // silently dropping data.
    expect(nextOnboardingCompletion("ready", "post-onboarding" as Phase, STEPS_PAID)).toEqual({
      step_name: "ready",
      step_index: 3,
    });
  });
});
