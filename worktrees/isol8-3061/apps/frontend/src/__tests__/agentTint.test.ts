import { describe, expect, it } from "vitest";
import { AGENT_TINT_COUNT, agentTint } from "@/lib/agentTint";

describe("agentTint", () => {
  it("is stable for the same id", () => {
    expect(agentTint("agent_7f3c")).toBe(agentTint("agent_7f3c"));
  });

  it("only ever returns a declared tint token", () => {
    const allowed = new Set(
      Array.from({ length: AGENT_TINT_COUNT }, (_, i) => `var(--agent-tint-${i + 1})`),
    );
    for (let i = 0; i < 200; i += 1) {
      expect(allowed.has(agentTint(`agent_${i}`))).toBe(true);
    }
  });

  it("separates ids that differ only in their last characters", () => {
    // The real input is `agent_<uuid>`, and a sum-of-char-codes hash would
    // collide freely across a shared prefix — which is exactly the case that
    // matters, since a roster's ids are generated together.
    const ids = [
      "agent_0b3f9c21-aaaa-4d11-9f01-000000000001",
      "agent_0b3f9c21-aaaa-4d11-9f01-000000000002",
      "agent_0b3f9c21-aaaa-4d11-9f01-000000000003",
    ];
    expect(new Set(ids.map(agentTint)).size).toBeGreaterThan(1);
  });

  it("spreads a realistic roster across every tint", () => {
    const seen = new Set(
      Array.from({ length: 60 }, (_, i) => agentTint(`agent_${i}${"x".repeat(i % 5)}`)),
    );
    expect(seen.size).toBe(AGENT_TINT_COUNT);
  });
});
