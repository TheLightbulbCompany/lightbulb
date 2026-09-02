import { describe, it, expect } from "vitest";
import { parseSessionKey, isSessionKeyForUser } from "../sessionKey";

describe("parseSessionKey", () => {
  it("parses personal webchat (agent:<id>:main)", () => {
    expect(parseSessionKey("agent:a1:main")).toEqual({
      agentId: "a1",
      source: "webchat",
    });
  });

  it("parses org webchat, member id is the third segment", () => {
    expect(parseSessionKey("agent:a1:user_abc")).toEqual({
      agentId: "a1",
      source: "webchat",
      memberId: "user_abc",
    });
  });

  it("parses a NAMED webchat session — member id is still the third segment", () => {
    // agent:<agentId>:<clerkUser>:<uuid> — a trailing-segment reading would
    // take the uuid and never match the signed-in user.
    expect(parseSessionKey("agent:a1:user_abc:2f9c-uuid")).toEqual({
      agentId: "a1",
      source: "webchat",
      memberId: "user_abc",
    });
  });

  it("classifies paperclip, cron and channel keys as non-webchat", () => {
    expect(parseSessionKey("agent:a1:paperclip")?.source).toBe("paperclip");
    expect(parseSessionKey("agent:a1:paperclip:issue:i7")?.source).toBe("paperclip");
    expect(parseSessionKey("agent:a1:cron:c1")?.source).toBe("cron");
    expect(parseSessionKey("agent:a1:slack:acc1:direct:p9")?.source).toBe("channel");
    expect(parseSessionKey("agent:a1:slack:group:g1")?.source).toBe("channel");
  });

  it("returns null for malformed keys", () => {
    expect(parseSessionKey("")).toBeNull();
    expect(parseSessionKey("nonsense")).toBeNull();
    expect(parseSessionKey("agent:a1")).toBeNull();
  });
});

describe("isSessionKeyForUser", () => {
  it("drops everything when not signed in", () => {
    expect(isSessionKeyForUser("agent:a1:main", null)).toBe(false);
  });

  it("accepts personal webchat regardless of user id", () => {
    expect(isSessionKeyForUser("agent:a1:main", "user_abc")).toBe(true);
  });

  it("accepts the signed-in member's own session, case-insensitively", () => {
    expect(isSessionKeyForUser("agent:a1:USER_ABC", "user_abc")).toBe(true);
    expect(isSessionKeyForUser("agent:a1:user_abc:uuid", "user_abc")).toBe(true);
  });

  it("drops another member's session (no prefix/suffix matching)", () => {
    expect(isSessionKeyForUser("agent:a1:user_abcd", "user_abc")).toBe(false);
    expect(isSessionKeyForUser("agent:a1:user_zzz:uuid", "user_abc")).toBe(false);
  });

  it("drops non-webchat sources — they belong to other surfaces", () => {
    expect(isSessionKeyForUser("agent:a1:paperclip", "user_abc")).toBe(false);
    expect(isSessionKeyForUser("agent:a1:cron:c1", "user_abc")).toBe(false);
  });

  it("accepts an absent/unparseable key (legacy emitters carry no scope)", () => {
    expect(isSessionKeyForUser(undefined, "user_abc")).toBe(true);
    expect(isSessionKeyForUser("garbage", "user_abc")).toBe(true);
  });
});
