import { describe, it, expect } from "vitest";
import { buildModelNameMap, displayModelName } from "../modelDisplay";

const ARN = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/ykpc0bdu467l";

const config = {
  models: {
    providers: {
      "amazon-bedrock": {
        models: [
          { id: ARN, name: "Claude Opus 4.6" },
          { id: "broken-entry" },
        ],
      },
    },
  },
};

describe("modelDisplay", () => {
  it("maps catalog ids (bare and provider-prefixed) to display names", () => {
    const names = buildModelNameMap(config);
    expect(names[ARN]).toBe("Claude Opus 4.6");
    expect(names[`amazon-bedrock/${ARN}`]).toBe("Claude Opus 4.6");
  });

  it("resolves a provider-prefixed ARN model to its catalog name", () => {
    const names = buildModelNameMap(config);
    expect(displayModelName(`amazon-bedrock/${ARN}`, names)).toBe("Claude Opus 4.6");
  });

  it("hides unresolvable ARNs instead of showing the opaque suffix", () => {
    // The pre-fix behavior rendered "ykpc0bdu467l" in the sidebar.
    expect(displayModelName(`amazon-bedrock/${ARN}`, {})).toBeNull();
    expect(displayModelName(ARN, undefined)).toBeNull();
  });

  it("keeps legacy readable ids on the old last-segment behavior", () => {
    expect(displayModelName("anthropic/claude-sonnet-4-6", {})).toBe("claude-sonnet-4-6");
    expect(displayModelName("us.anthropic.claude-haiku-4-5-v1:0", {})).toBe("us.anthropic.claude-haiku-4-5");
  });

  it("returns null for empty input and tolerates malformed config", () => {
    expect(displayModelName(undefined, {})).toBeNull();
    expect(buildModelNameMap(null)).toEqual({});
    expect(buildModelNameMap({ models: { providers: "junk" } })).toEqual({});
  });
});
