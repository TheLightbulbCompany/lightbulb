import { shouldSuppressBanner } from "./notifications";
import { sessionIdFromKey } from "@lightbulb/shared";

const SID = "0f6b2f9a-1234-4abc-8def-0123456789ab";

describe("shouldSuppressBanner", () => {
  it("suppresses on the same agent + same General session", () => {
    expect(
      shouldSuppressBanner("agent_1", null, "agent_1", "agent:agent_1:user_x"),
    ).toBe(true);
  });
  it("suppresses on the same agent + same named session", () => {
    expect(
      shouldSuppressBanner("agent_1", SID, "agent_1", `agent:agent_1:user_x:${SID}`),
    ).toBe(true);
  });
  it("shows for a different session of the same agent (its text is not on screen)", () => {
    expect(
      shouldSuppressBanner("agent_1", null, "agent_1", `agent:agent_1:user_x:${SID}`),
    ).toBe(false);
    expect(
      shouldSuppressBanner("agent_1", SID, "agent_1", "agent:agent_1:user_x"),
    ).toBe(false);
  });
  it("shows when on a different agent", () => {
    expect(shouldSuppressBanner("agent_2", null, "agent_1", "agent:agent_1:user_x")).toBe(false);
  });
  it("shows when not on any agent chat", () => {
    expect(shouldSuppressBanner(null, null, "agent_1", "agent:agent_1:user_x")).toBe(false);
  });
  it("shows when the notif carries no agent id", () => {
    expect(shouldSuppressBanner("agent_1", null, undefined, undefined)).toBe(false);
  });
  it("falls back to agent-scoped suppression for legacy payloads without a sessionKey", () => {
    expect(shouldSuppressBanner("agent_1", null, "agent_1", undefined)).toBe(true);
  });
});

describe("sessionIdFromKey", () => {
  it("extracts the id from a 4-part key", () => {
    expect(sessionIdFromKey(`agent:a1:user_x:${SID}`)).toBe(SID);
  });
  it("returns null for a 3-part/General key (member id is not a session)", () => {
    expect(sessionIdFromKey("agent:a1:user_x")).toBeNull();
  });
  it("returns null for cron and malformed keys", () => {
    expect(sessionIdFromKey("agent:a1:cron:c1")).toBeNull();
    expect(sessionIdFromKey(undefined)).toBeNull();
    expect(sessionIdFromKey("")).toBeNull();
  });
});
