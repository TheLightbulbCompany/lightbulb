import { describe, expect, it } from "vitest";

import { getAgentModelString } from "./agents";

describe("getAgentModelString", () => {
  it("normalizes both ECS model-ref shapes, untouched", () => {
    expect(getAgentModelString({ model: "amazon-bedrock/claude-sonnet-4" })).toBe(
      "amazon-bedrock/claude-sonnet-4",
    );
    expect(getAgentModelString({ model: { primary: "amazon-bedrock/claude-opus-4" } })).toBe(
      "amazon-bedrock/claude-opus-4",
    );
    expect(getAgentModelString({ model: "  openai/gpt-5  " })).toBe("openai/gpt-5");
  });

  it("renders the AgentCore tier vocabulary as a label (M6 task 10, row 29)", () => {
    // A migrated workspace's roster rows carry a TIER, never a ref — rendering the raw
    // "premium" would read as a model name the owner could go looking for.
    expect(getAgentModelString({ model: "standard" })).toBe("Standard");
    expect(getAgentModelString({ model: "premium" })).toBe("Premium");
    expect(getAgentModelString({ model: { primary: "premium" } })).toBe("Premium");
  });

  it("is undefined when there is no model at all", () => {
    expect(getAgentModelString(undefined)).toBeUndefined();
    expect(getAgentModelString({})).toBeUndefined();
    expect(getAgentModelString({ model: "   " })).toBeUndefined();
    expect(getAgentModelString({ model: { fallbacks: ["x"] } })).toBeUndefined();
  });
});
