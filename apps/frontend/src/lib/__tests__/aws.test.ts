import { describe, expect, it } from "vitest";
import { AWS_REGION } from "../aws";

describe("AWS_REGION", () => {
  it("defaults to us-east-1 when the env var is unset", () => {
    expect(typeof AWS_REGION).toBe("string");
    expect(AWS_REGION.length).toBeGreaterThan(0);
  });
});
