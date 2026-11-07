import { describe, it, expect } from "vitest";

import { classifyAgentStatus } from "../agentStatus";

describe("classifyAgentStatus", () => {
  it("treats every live-meaning status as live, not just 'running'", () => {
    // The roster's own map recognised only `running` and bucketed these into
    // idle — so an agent reporting `active` or `thinking` rendered as asleep
    // while the org chart, reading the same field, showed it as live.
    for (const status of ["active", "online", "running", "thinking", "covered", "stalled"]) {
      expect(classifyAgentStatus(status)).toBe("live");
    }
  });

  it("treats 'failed' as needing a human, exactly like 'error'", () => {
    expect(classifyAgentStatus("error")).toBe("attention");
    expect(classifyAgentStatus("failed")).toBe("attention");
  });

  it("keeps paused and terminated distinct", () => {
    // `terminated` is "not a usable agent" (TERMINATED_AGENT_STATUS) and is
    // filtered out of the Teams pickers; `paused` is one click from resuming.
    expect(classifyAgentStatus("paused")).toBe("paused");
    expect(classifyAgentStatus("terminated")).toBe("terminated");
  });

  it("is case-insensitive", () => {
    expect(classifyAgentStatus("RUNNING")).toBe("live");
  });

  it("returns null — never a guess — for absent or unrecognised statuses", () => {
    // Callers must decide for themselves. A shared default is what let a live
    // agent silently classify as idle.
    expect(classifyAgentStatus(null)).toBeNull();
    expect(classifyAgentStatus(undefined)).toBeNull();
    expect(classifyAgentStatus("")).toBeNull();
    expect(classifyAgentStatus("compacting")).toBeNull();
  });
});
