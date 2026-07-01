import { describe, it, expect } from "vitest";
import { buildChatSessionKey } from "@/lib/sessionKey";

describe("buildChatSessionKey", () => {
  it("returns the legacy 3-part key for the General session (null id)", () => {
    expect(buildChatSessionKey("agent_a", "user_1", null)).toBe("agent:agent_a:user_1");
  });
  it("returns a 4-part key for a valid lowercase UUID", () => {
    const id = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";
    expect(buildChatSessionKey("agent_a", "user_1", id)).toBe(
      `agent:agent_a:user_1:${id}`,
    );
  });
  it("rejects colon-injection and reserved-token ids", () => {
    expect(() => buildChatSessionKey("agent_a", "user_1", "foo:heartbeat")).toThrow();
    expect(() => buildChatSessionKey("agent_a", "user_1", "NOT-A-UUID")).toThrow();
    // Uppercase UUID would alias-merge under OpenClaw's lowercase fold.
    expect(() =>
      buildChatSessionKey("agent_a", "user_1", "3F2504E0-4F89-41D3-9A0C-0305E82C3301"),
    ).toThrow();
  });
});
