import { shouldSuppressBanner } from "./notifications";
import { sessionIdFromKey } from "@lightbulb/shared";

const SID = "0f6b2f9a-1234-4abc-8def-0123456789ab";

/** The on-screen target, defaulted to "nothing on screen". */
const on = (over: Partial<Record<"agentId" | "sessionId" | "roomId" | "threadId", string>> = {}) => ({
  agentId: null,
  sessionId: null,
  roomId: null,
  threadId: null,
  ...over,
});

describe("shouldSuppressBanner", () => {
  it("suppresses on the same agent + same General session", () => {
    expect(
      shouldSuppressBanner(on({ agentId: "agent_1" }), {
        agentId: "agent_1",
        sessionKey: "agent:agent_1:user_x",
      }),
    ).toBe(true);
  });
  it("suppresses on the same agent + same named session", () => {
    expect(
      shouldSuppressBanner(on({ agentId: "agent_1", sessionId: SID }), {
        agentId: "agent_1",
        sessionKey: `agent:agent_1:user_x:${SID}`,
      }),
    ).toBe(true);
  });
  it("shows for a different session of the same agent (its text is not on screen)", () => {
    expect(
      shouldSuppressBanner(on({ agentId: "agent_1" }), {
        agentId: "agent_1",
        sessionKey: `agent:agent_1:user_x:${SID}`,
      }),
    ).toBe(false);
    expect(
      shouldSuppressBanner(on({ agentId: "agent_1", sessionId: SID }), {
        agentId: "agent_1",
        sessionKey: "agent:agent_1:user_x",
      }),
    ).toBe(false);
  });
  it("shows when on a different agent", () => {
    expect(
      shouldSuppressBanner(on({ agentId: "agent_2" }), {
        agentId: "agent_1",
        sessionKey: "agent:agent_1:user_x",
      }),
    ).toBe(false);
  });
  it("shows when not on any agent chat", () => {
    expect(
      shouldSuppressBanner(on(), { agentId: "agent_1", sessionKey: "agent:agent_1:user_x" }),
    ).toBe(false);
  });
  it("shows when the notif carries no agent id", () => {
    expect(shouldSuppressBanner(on({ agentId: "agent_1" }), {})).toBe(false);
  });
  it("falls back to agent-scoped suppression for legacy payloads without a sessionKey", () => {
    expect(shouldSuppressBanner(on({ agentId: "agent_1" }), { agentId: "agent_1" })).toBe(true);
  });

  it("suppresses a room push while that conversation is on screen", () => {
    expect(
      shouldSuppressBanner(on({ roomId: "r1" }), { roomId: "r1", threadRootMessageId: null }),
    ).toBe(true);
  });
  it("shows a room push for a different conversation", () => {
    expect(
      shouldSuppressBanner(on({ roomId: "r2" }), { roomId: "r1", threadRootMessageId: null }),
    ).toBe(false);
  });
  it("shows a thread reply while only the parent conversation is on screen", () => {
    expect(
      shouldSuppressBanner(on({ roomId: "r1" }), { roomId: "r1", threadRootMessageId: "m1" }),
    ).toBe(false);
  });
  it("suppresses a thread reply while that thread is on screen", () => {
    expect(
      shouldSuppressBanner(on({ roomId: "r1", threadId: "m1" }), {
        roomId: "r1",
        threadRootMessageId: "m1",
      }),
    ).toBe(true);
  });
  // A room push wins over a stale agent target: the two halves are written by
  // different screens and only one of them can be on screen.
  it("shows a room push while an agent chat is the recorded target", () => {
    expect(
      shouldSuppressBanner(on({ agentId: "agent_1" }), {
        agentId: "agent_1",
        roomId: "r1",
        threadRootMessageId: null,
      }),
    ).toBe(false);
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
