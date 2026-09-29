import { describe, it, expect } from "vitest";

import { sessionIdFromKey } from "./sessionKey";

describe("sessionIdFromKey", () => {
  const id = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";

  it("extracts the id from a 4-part key", () => {
    expect(sessionIdFromKey(`agent:agent_a:user_1:${id}`)).toBe(id);
  });
  it("returns null for a 3-part General key", () => {
    expect(sessionIdFromKey("agent:agent_a:user_1")).toBeNull();
  });
  it("returns null for a non-session trailing segment (cron / member ids)", () => {
    expect(sessionIdFromKey("agent:agent_a:user_1:cron_abc")).toBeNull();
    expect(sessionIdFromKey("agent:agent_a:user_1:user_2")).toBeNull();
  });
  it("returns null for nullish input", () => {
    expect(sessionIdFromKey(null)).toBeNull();
    expect(sessionIdFromKey(undefined)).toBeNull();
    expect(sessionIdFromKey("")).toBeNull();
  });
});
