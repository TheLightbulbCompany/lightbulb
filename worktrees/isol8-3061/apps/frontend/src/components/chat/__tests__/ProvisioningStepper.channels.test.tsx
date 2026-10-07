import { describe, it, expect } from "vitest";
import * as Stepper from "../ProvisioningStepper";

describe("ProvisioningStepper has no forced channels phase", () => {
  it("does not divert to a channels wizard once the agent step is complete", () => {
    // Channel setup is no longer forced before chat — it is nudged via the
    // Get Started guide and remains available in Settings → Channels. The
    // post-agent phase derivation must always resolve to "ready", regardless
    // of whether a main-agent bot exists.
    expect(Stepper.resolvePostAgentPhase()).toBe("ready");
    expect(Stepper.resolvePostAgentPhase()).toBe("ready");
  });
});
