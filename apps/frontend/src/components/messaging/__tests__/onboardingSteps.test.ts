import { describe, expect, it } from "vitest";

import { buildSetupSteps, isBandComplete, STARTER_AGENT_DEFAULT_NAMES } from "../onboardingSteps";
import type { Connection } from "@/hooks/useConnections";
import type { RoomParticipant } from "../types";

const connection = (provider: string, status: Connection["status"]): Connection => ({
  provider,
  display_name: provider,
  acquisition: "direct_oauth",
  status,
});

const ALL_CONNECTED = ["stripe", "github", "notion", "posthog"].map((p) => connection(p, "connected"));

const participant = (kind: "human" | "agent", id: string, displayName: string): RoomParticipant => ({
  kind,
  id,
  displayName,
  mentionHandle: kind === "agent" ? displayName.toLowerCase() : null,
  role: "member",
  listenMode: null,
  joinedAt: "2026-09-20T00:00:00Z",
  joinedSeq: 0,
});

const STARTER_SIX = [...STARTER_AGENT_DEFAULT_NAMES].map((name, i) => participant("agent", `agent_${i}`, name));
const SOLO = [participant("human", "user_a", "Aden"), ...STARTER_SIX];

const step = (
  participants: readonly RoomParticipant[],
  connections: readonly Connection[],
  id: string,
  agentNames: ReadonlyMap<string, string> = new Map(),
) => buildSetupSteps({ participants, connections, agentNames }).find((s) => s.id === id)!;

describe("connector steps", () => {
  it("is done once the provider reports connected", () => {
    expect(step(SOLO, [connection("stripe", "connected")], "stripe").done).toBe(true);
  });

  it("is not done while the provider is disconnected", () => {
    expect(step(SOLO, [connection("stripe", "disconnected")], "stripe").done).toBe(false);
  });

  it("is not done when the provider is missing from the catalog entirely", () => {
    expect(step(SOLO, [], "github").done).toBe(false);
  });

  it("offers exactly stripe, github, notion and shopify", () => {
    expect(buildSetupSteps({ participants: SOLO, connections: [], agentNames: new Map() }).map((s) => s.id))
      .toEqual(["stripe", "github", "notion", "posthog", "invite", "name-agents"]);
  });
});

describe("invite step", () => {
  it("is done once a second human is in the room", () => {
    const withTeammate = [...SOLO, participant("human", "user_b", "Sam")];
    expect(step(withTeammate, [], "invite").done).toBe(true);
  });

  it("is not done while the owner is the only human", () => {
    expect(step(SOLO, [], "invite").done).toBe(false);
  });

  it("does not count agents as teammates", () => {
    expect(step([participant("human", "user_a", "Aden"), ...STARTER_SIX], [], "invite").done).toBe(false);
  });
});

describe("name-agents step", () => {
  it("is not done while every agent still carries its seeded role name", () => {
    expect(step(SOLO, [], "name-agents").done).toBe(false);
  });

  it("is done as soon as one agent has been renamed", () => {
    const renamed = [participant("human", "user_a", "Aden"), participant("agent", "agent_0", "Ada"), ...STARTER_SIX.slice(1)];
    expect(step(renamed, [], "name-agents").done).toBe(true);
  });

  it("reads the live roster, so a rename in Teams flips the card immediately", () => {
    // The room's member row still says "CEO" — upsert_member wrote it at seed
    // time and PATCH /teams/agents/{id} never rewrites it.
    const live = new Map([["agent_0", "Ada"]]);
    expect(step(SOLO, [], "name-agents", live).done).toBe(true);
  });

  it("falls back to the member row when the roster has not loaded yet", () => {
    expect(step(SOLO, [], "name-agents", new Map()).done).toBe(false);
  });

  it("ignores a renamed agent that is not in this room, so a marketplace hire cannot finish it", () => {
    const live = new Map([["agent_hired", "Scout"]]);
    expect(step(SOLO, [], "name-agents", live).done).toBe(false);
  });

  it("ignores humans, whose names are never seeded defaults", () => {
    expect(step([participant("human", "user_a", "Aden")], [], "name-agents").done).toBe(false);
  });
});

describe("permissions", () => {
  it("marks every connector and the invite as admin-only", () => {
    const steps = buildSetupSteps({ participants: SOLO, connections: [], agentNames: new Map() });
    expect(steps.filter((s) => s.adminOnly).map((s) => s.id))
      .toEqual(["stripe", "github", "notion", "posthog", "invite"]);
  });

  it("leaves naming open to members, since PATCH /teams/agents has no admin guard", () => {
    expect(step(SOLO, [], "name-agents").adminOnly).toBe(false);
  });
});

describe("isBandComplete", () => {
  it("is false while any step is outstanding", () => {
    expect(isBandComplete(buildSetupSteps({ participants: SOLO, connections: ALL_CONNECTED, agentNames: new Map() }))).toBe(false);
  });

  it("is true once every step is done", () => {
    const participants = [
      participant("human", "user_a", "Aden"),
      participant("human", "user_b", "Sam"),
      participant("agent", "agent_0", "Ada"),
    ];
    expect(isBandComplete(buildSetupSteps({ participants, connections: ALL_CONNECTED, agentNames: new Map() }))).toBe(true);
  });
});
