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

  it("passes an AgentCore model id through untouched (M6 task 10-C, row 29)", () => {
    // A migrated workspace's roster rows carry the profile's own `model.default` — a real id
    // `models.list` names. The transitional tier words ("standard"/"premium") are gone, and
    // nothing here rewrites an id into a label: `buildModelNameMap` does that from the list.
    expect(getAgentModelString({ model: "arn:aws:bedrock:us-east-1:1:application-inference-profile/x" })).toBe(
      "arn:aws:bedrock:us-east-1:1:application-inference-profile/x",
    );
    expect(getAgentModelString({ model: "claude-sonnet-5" })).toBe("claude-sonnet-5");
  });

  it("is undefined when there is no model at all", () => {
    expect(getAgentModelString(undefined)).toBeUndefined();
    expect(getAgentModelString({})).toBeUndefined();
    expect(getAgentModelString({ model: "   " })).toBeUndefined();
    expect(getAgentModelString({ model: { fallbacks: ["x"] } })).toBeUndefined();
  });
});
