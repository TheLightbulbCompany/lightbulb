import { describe, it, expect } from "vitest";
import {
  PROVISION_IDEAS,
  PROVISION_IDEA_ROTATION_MS,
  PROVISION_STUCK_MS,
  PROVISION_TIMEOUT_MS,
  formatProvisionElapsed,
} from "./provisioningCopy";

describe("formatProvisionElapsed", () => {
  it("formats m:ss under an hour", () => {
    expect(formatProvisionElapsed(0)).toBe("0:00");
    expect(formatProvisionElapsed(1000)).toBe("0:01");
    expect(formatProvisionElapsed(59_000)).toBe("0:59");
    expect(formatProvisionElapsed(60_000)).toBe("1:00");
    expect(formatProvisionElapsed(305_000)).toBe("5:05");
    expect(formatProvisionElapsed(3_599_000)).toBe("59:59");
  });
  // A 21-hour dev stall rendered as "1260:37" — a four-digit minute count
  // next to the words "cold starts can take a few minutes".
  it("rolls over to h:mm:ss past an hour", () => {
    expect(formatProvisionElapsed(3_600_000)).toBe("1:00:00");
    expect(formatProvisionElapsed(3_661_000)).toBe("1:01:01");
    expect(formatProvisionElapsed(75_637_000)).toBe("21:00:37");
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
    expect(PROVISION_STUCK_MS).toBe(900_000);
    expect(PROVISION_IDEA_ROTATION_MS).toBe(3200);
  });
  it("the stuck tier is strictly later than the soft timeout", () => {
    expect(PROVISION_STUCK_MS).toBeGreaterThan(PROVISION_TIMEOUT_MS);
  });
});
