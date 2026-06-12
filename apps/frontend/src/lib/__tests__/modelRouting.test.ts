import { describe, it, expect } from "vitest";
import {
  buildAgentHeartbeatPatch,
  buildDefaultModelPatch,
  buildOverheadModelPatch,
  readAgentHeartbeat,
  readDefaultHeartbeat,
  readDefaultModelPrimary,
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
