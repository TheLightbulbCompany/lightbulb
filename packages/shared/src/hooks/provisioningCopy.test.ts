import { describe, it, expect } from "vitest";
import {
  PROVISION_IDEAS,
  PROVISION_IDEA_ROTATION_MS,
  PROVISION_TIMEOUT_MS,
  formatProvisionElapsed,
} from "./provisioningCopy";

describe("formatProvisionElapsed", () => {
  it("formats mm:ss", () => {
    expect(formatProvisionElapsed(0)).toBe("0:00");
    expect(formatProvisionElapsed(1000)).toBe("0:01");
    expect(formatProvisionElapsed(59_000)).toBe("0:59");
    expect(formatProvisionElapsed(60_000)).toBe("1:00");
    expect(formatProvisionElapsed(305_000)).toBe("5:05");
  });
  it("clamps negatives to 0:00", () => {
    expect(formatProvisionElapsed(-5000)).toBe("0:00");
  });
});

describe("provision copy constants", () => {
  it("ideas are non-empty and single-line-short", () => {
    expect(PROVISION_IDEAS.length).toBeGreaterThan(0);
    for (const idea of PROVISION_IDEAS) expect(idea.length).toBeLessThanOrEqual(60);
  });
  it("timing constants", () => {
    expect(PROVISION_TIMEOUT_MS).toBe(300_000);
    expect(PROVISION_IDEA_ROTATION_MS).toBe(3200);
  });
});
