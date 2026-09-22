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

const ALL_CONNECTED = ["stripe", "github", "notion"].map((p) => connection(p, "connected"));
const CATALOG = ["stripe", "github", "notion"].map((p) => connection(p, "disconnected"));

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
  workspaceMembers = 1,
) => buildSetupSteps({ participants, connections, agentNames, workspaceMembers }).find((s) => s.id === id)!;

const build = (connections: readonly Connection[], participants: readonly RoomParticipant[] = SOLO, workspaceMembers = 1) =>
  buildSetupSteps({ participants, connections, agentNames: new Map(), workspaceMembers });

describe("connector steps", () => {
  it("is done once the provider reports connected", () => {
    expect(step(SOLO, [connection("stripe", "connected")], "stripe").done).toBe(true);
  });

  it("is not done while the provider is disconnected", () => {
    expect(step(SOLO, [connection("stripe", "disconnected")], "stripe").done).toBe(false);
  });

  it("offers exactly stripe, github and notion", () => {
    expect(build(CATALOG).map((s) => s.id)).toEqual(["stripe", "github", "notion", "invite", "name-agents"]);
  });

  it("drops a connector this owner cannot connect, rather than offering a dead card", () => {
    // "unavailable" covers both a gated provider and one this runtime cannot
    // deliver; the authorize leg would refuse either.
    const connections = [connection("stripe", "unavailable"), ...CATALOG.slice(1)];
    expect(build(connections).map((s) => s.id)).toEqual(["github", "notion", "invite", "name-agents"]);
  });

  it("drops a connector missing from the catalog entirely", () => {
    expect(build(CATALOG.slice(0, 2)).map((s) => s.id)).not.toContain("notion");
  });
});

describe("invite step", () => {
  it("is done once the workspace has a second member", () => {
    expect(step(SOLO, [], "invite", new Map(), 2).done).toBe(true);
  });

  it("is not done while the owner is the only member", () => {
    expect(step(SOLO, [], "invite", new Map(), 1).done).toBe(false);
  });

  it("reads workspace membership, not #general, which nothing adds a new member to", () => {
    const withTeammate = [...SOLO, participant("human", "user_b", "Sam")];
    expect(step(withTeammate, [], "invite", new Map(), 1).done).toBe(false);
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
    const steps = build(CATALOG);
    expect(steps.filter((s) => s.adminOnly).map((s) => s.id))
      .toEqual(["stripe", "github", "notion", "invite"]);
  });

  it("leaves naming open to members, since PATCH /teams/agents has no admin guard", () => {
    expect(step(SOLO, [], "name-agents").adminOnly).toBe(false);
  });
});

describe("isBandComplete", () => {
  it("is false while any step is outstanding", () => {
    expect(isBandComplete(build(ALL_CONNECTED))).toBe(false);
  });

  it("is true once every step is done", () => {
    const participants = [participant("human", "user_a", "Aden"), participant("agent", "agent_0", "Ada")];
    expect(isBandComplete(build(ALL_CONNECTED, participants, 2))).toBe(true);
  });

  it("does not wait on a connector it dropped as unavailable", () => {
    const participants = [participant("human", "user_a", "Aden"), participant("agent", "agent_0", "Ada")];
    const connections = [connection("stripe", "unavailable"), ...ALL_CONNECTED.slice(1)];
    expect(isBandComplete(build(connections, participants, 2))).toBe(true);
  });
});
