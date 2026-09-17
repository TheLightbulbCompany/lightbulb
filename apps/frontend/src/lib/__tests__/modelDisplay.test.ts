import { describe, it, expect } from "vitest";
import { buildModelNameMap, displayModelName, listCatalogModels } from "../modelDisplay";

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

/**
 * M6 task 10-C (scan rows 2/27). On AgentCore there is no providers catalog to walk — a Hermes
 * config has no `models` key at all — so the same walkers take the `models.list` payload: the
 * owner's own Bedrock inference profiles, or Hermes's catalog via the bridge.
 */
describe("modelDisplay over the models.list payload", () => {
  const list = {
    models: [
      { id: ARN, name: "Claude Opus", provider: "bedrock", tier: "premium" },
      { id: "claude-sonnet-5", name: "claude-sonnet-5", provider: "anthropic" },
      { id: "", name: "dropped" },
    ],
    defaultId: ARN,
  };

  it("names an AIP ARN, so the sidebar stops rendering the opaque suffix", () => {
    const names = buildModelNameMap(list);
    expect(displayModelName(ARN, names)).toBe("Claude Opus");
  });

  it("offers each id verbatim as the ref the picker sends back", () => {
    // No provider prefix is rebuilt: a Hermes profile stores `model.default` bare, and the
    // backend refuses any id it did not itself offer.
    expect(listCatalogModels(list)).toEqual([
      { ref: ARN, bareId: ARN, name: "Claude Opus" },
      { ref: "claude-sonnet-5", bareId: "claude-sonnet-5", name: "claude-sonnet-5" },
    ]);
  });

  it("is empty, never a providers walk, for an empty list", () => {
    expect(listCatalogModels({ models: [] })).toEqual([]);
    expect(buildModelNameMap({ models: [] })).toEqual({});
  });
});
