import { describe, it, expect } from "vitest";
import {
  buildAgentHeartbeatPatch,
  buildDefaultModelPatch,
  buildHermesOverheadModelPatch,
  buildOverheadModelPatch,
  readAgentHeartbeat,
  readDefaultHeartbeat,
  readDefaultModelPrimary,
  readHermesOverheadModel,
  readOverheadModel,
} from "../modelRouting";

const OPUS = "amazon-bedrock/arn:aws:bedrock:us-east-1:1:application-inference-profile/op";
const HAIKU = "amazon-bedrock/arn:aws:bedrock:us-east-1:1:application-inference-profile/hk";

const config = {
  agents: {
    defaults: {
      model: { primary: "amazon-bedrock/sonnet-arn", fallbacks: [HAIKU] },
      heartbeat: { every: "30m", model: HAIKU },
      compaction: { model: HAIKU, memoryFlush: { model: HAIKU } },
    },
    list: [
      { id: "friday", model: OPUS, heartbeat: { every: "2h", activeHours: { start: "09:00", end: "18:00" } } },
      { id: "atlas" },
    ],
  },
};

describe("modelRouting readers", () => {
  it("reads per-agent heartbeat overrides without merging defaults", () => {
    expect(readAgentHeartbeat(config, "friday")).toEqual({
      every: "2h",
      activeHours: { start: "09:00", end: "18:00" },
    });
    // Atlas has no override — empty, the UI shows inherit placeholders.
    expect(readAgentHeartbeat(config, "atlas")).toEqual({});
  });

  it("reads defaults for inherit placeholders", () => {
    expect(readDefaultHeartbeat(config)).toEqual({ every: "30m", model: HAIKU });
    expect(readDefaultModelPrimary(config)).toBe("amazon-bedrock/sonnet-arn");
    expect(readOverheadModel(config)).toBe(HAIKU);
  });

  it("tolerates string-form default model and malformed config", () => {
    expect(readDefaultModelPrimary({ agents: { defaults: { model: "x/y" } } })).toBe("x/y");
    expect(readDefaultModelPrimary(null)).toBeUndefined();
    expect(readAgentHeartbeat(undefined, "a")).toEqual({});
  });
});

describe("modelRouting patch builders", () => {
  it("builds a per-agent heartbeat patch that nulls unset fields", () => {
    // Nulls delete the keys under RFC 7386 merge semantics — without them a
    // previously-set override would silently survive a revert-to-inherit.
    expect(buildAgentHeartbeatPatch("friday", { every: "1h" })).toEqual({
      agents: { list: [{ id: "friday", heartbeat: { every: "1h", model: null, activeHours: null } }] },
    });
  });

  it("nulls the whole block when everything inherits", () => {
    expect(buildAgentHeartbeatPatch("friday", {})).toEqual({
      agents: { list: [{ id: "friday", heartbeat: null }] },
    });
  });

  it("carries activeHours through", () => {
    expect(
      buildAgentHeartbeatPatch("a", { model: HAIKU, activeHours: { start: "08:00", end: "20:00" } }),
    ).toEqual({
      agents: {
        list: [
          { id: "a", heartbeat: { every: null, model: HAIKU, activeHours: { start: "08:00", end: "20:00" } } },
        ],
      },
    });
  });

  it("default-model patch touches only primary (fallbacks preserved by omission)", () => {
    expect(buildDefaultModelPatch(OPUS)).toEqual({
      agents: { defaults: { model: { primary: OPUS } } },
    });
  });

  it("overhead patch routes compaction + memoryFlush + heartbeat model together", () => {
    expect(buildOverheadModelPatch(HAIKU)).toEqual({
      agents: {
        defaults: {
          compaction: { model: HAIKU, memoryFlush: { model: HAIKU } },
          heartbeat: { model: HAIKU },
        },
      },
    });
  });
});

/**
 * M6 task 10-C (scan rows 4/23). Hermes has no `agents.defaults` — overhead work is the
 * `auxiliary.<task>` block. The browser sends ONE id and the backend expands it across every
 * task with the provider its own writer derives; a block built here would drift from the one
 * provisioning writes, and the browser knows neither the task list nor the provider.
 */
describe("modelRouting on AgentCore", () => {
  const AIP_HAIKU = "arn:aws:bedrock:us-east-1:1:application-inference-profile/hk";

  it("sends one id, never a hand-built auxiliary block", () => {
    expect(buildHermesOverheadModelPatch(AIP_HAIKU)).toEqual({ auxiliary: { model: AIP_HAIKU } });
  });

  it("reads the pinned model back off any auxiliary task", () => {
    expect(
      readHermesOverheadModel({
        auxiliary: {
          free_only: true,
          transient_retries: 2,
          compression: { provider: "bedrock", model: AIP_HAIKU, fallback_chain: [] },
          curator: { provider: "bedrock", model: AIP_HAIKU, fallback_chain: [] },
        },
      }),
    ).toBe(AIP_HAIKU);
  });

  it("is undefined — not a crash — for a profile with no auxiliary block", () => {
    expect(readHermesOverheadModel({})).toBeUndefined();
    expect(readHermesOverheadModel(null)).toBeUndefined();
    expect(readHermesOverheadModel({ auxiliary: { free_only: true } })).toBeUndefined();
  });
});
