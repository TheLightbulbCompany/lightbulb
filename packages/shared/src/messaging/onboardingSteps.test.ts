import { describe, expect, it } from "vitest";

import {
  buildSetupSteps,
  isBandComplete,
  setupProgress,
  STARTER_AGENT_DEFAULT_NAMES,
  type SetupConnection as Connection,
  type SetupParticipant as RoomParticipant,
} from "./onboardingSteps";

const connection = (provider: string, status: string): Connection => ({ provider, status });

const ALL_CONNECTED = ["stripe", "github", "notion"].map((p) => connection(p, "connected"));
const CATALOG = ["stripe", "github", "notion"].map((p) => connection(p, "disconnected"));

const participant = (kind: "human" | "agent", id: string, displayName: string): RoomParticipant => ({ kind, id, displayName });

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
    expect(build(CATALOG).map((s) => s.id)).toEqual(["invite", "stripe", "github", "notion", "name-agents"]);
  });

  it("drops a connector this owner cannot connect, rather than offering a dead card", () => {
    // "unavailable" covers both a gated provider and one this runtime cannot
    // deliver; the authorize leg would refuse either.
    const connections = [connection("stripe", "unavailable"), ...CATALOG.slice(1)];
    expect(build(connections).map((s) => s.id)).toEqual(["invite", "github", "notion", "name-agents"]);
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
  it("marks every connector as admin-only, and leaves the invite open to members (D5)", () => {
    const steps = build(CATALOG);
    expect(steps.filter((s) => s.adminOnly).map((s) => s.id))
      .toEqual(["stripe", "github", "notion"]);
  });

  it("leaves naming open to members, since PATCH /teams/agents has no admin guard", () => {
    expect(step(SOLO, [], "name-agents").adminOnly).toBe(false);
  });
});

describe("isBandComplete", () => {
  it("is false while any counted step is outstanding", () => {
    expect(isBandComplete(build(ALL_CONNECTED))).toBe(false);
  });

  it("is true once the invite and one tool are done, without waiting on the later steps", () => {
    expect(isBandComplete(build([connection("stripe", "connected"), ...CATALOG.slice(1)], SOLO, 2))).toBe(true);
  });

  it("does not wait on a connector it dropped as unavailable", () => {
    const participants = [participant("human", "user_a", "Aden"), participant("agent", "agent_0", "Ada")];
    const connections = [connection("stripe", "unavailable"), ...ALL_CONNECTED.slice(1)];
    expect(isBandComplete(build(connections, participants, 2))).toBe(true);
  });
});

describe("08 §3 order and progress", () => {
  const full = (inputs: { humanSpoke: boolean; approvalAnswered: boolean; members?: number; connected?: boolean }) =>
    buildSetupSteps({
      participants: SOLO,
      connections: inputs.connected ? [...CATALOG, connection("github", "connected")] : CATALOG,
      agentNames: new Map(),
      workspaceMembers: inputs.members ?? 1,
      contextImported: false,
      humanSpoke: inputs.humanSpoke,
      approvalAnswered: inputs.approvalAnswered,
    });

  it("puts the invite first and naming and context last, as later steps", () => {
    const steps = full({ humanSpoke: false, approvalAnswered: false });
    expect(steps.map((s) => s.id)).toEqual(["invite", "tell-ceo", "stripe", "github", "notion", "approval", "name-agents", "context"]);
    expect(steps.filter((s) => s.later).map((s) => s.id)).toEqual(["name-agents", "context"]);
    expect(steps[0]).toMatchObject({ label: "Invite your team", adminOnly: false });
  });

  it("starts at 2 of 6 (the workspace and its AI employees) and counts the tools as one", () => {
    expect(setupProgress(full({ humanSpoke: false, approvalAnswered: false }))).toEqual({ done: 2, total: 6 });
    expect(setupProgress(full({ humanSpoke: true, approvalAnswered: false, connected: true }))).toEqual({ done: 4, total: 6 });
    const all = full({ humanSpoke: true, approvalAnswered: true, connected: true, members: 2 });
    expect(setupProgress(all)).toEqual({ done: 6, total: 6 });
    expect(isBandComplete(all)).toBe(true);
  });

  it("omits a step whose signal the client does not supply", () => {
    expect(build(CATALOG).some((s) => s.id === "tell-ceo" || s.id === "approval")).toBe(false);
  });
});

describe("starter-model step", () => {
  const paid = (plan: "base" | "standard" | "ultra_pro", provider_choice: string | null = null) => ({
    is_subscribed: true, subscription_status: "active", plan, provider_choice,
  });
  const withBilling = (billing: Parameters<typeof buildSetupSteps>[0]["billing"]) =>
    buildSetupSteps({ participants: SOLO, connections: CATALOG, agentNames: new Map(), workspaceMembers: 1, billing });

  it.each(["standard", "ultra_pro"] as const)("leads the band for a paid %s workspace with no provider", (plan) => {
    const steps = withBilling(paid(plan));
    expect(steps[0]).toMatchObject({
      id: "starter-model",
      label: "You're on the starter model",
      detail: "Choose OpenRouter managed or sign in with ChatGPT",
      done: false,
      adminOnly: true,
    });
  });

  it("is absent on Base, whose managed model is the plan", () => {
    expect(withBilling(paid("base")).some((s) => s.id === "starter-model")).toBe(false);
  });

  it("is absent on the free trial, even though the backend defaults its plan to standard", () => {
    const trial = { is_subscribed: false, subscription_status: "trial_local", plan: "standard" as const, provider_choice: null };
    expect(withBilling(trial).some((s) => s.id === "starter-model")).toBe(false);
  });

  it("disappears once a provider is chosen", () => {
    expect(withBilling(paid("standard", "bedrock_claude")).some((s) => s.id === "starter-model")).toBe(false);
  });

  it("is absent while billing is unknown", () => {
    expect(withBilling(null).some((s) => s.id === "starter-model")).toBe(false);
    expect(withBilling(undefined).some((s) => s.id === "starter-model")).toBe(false);
  });

  it("leaves the other steps unchanged", () => {
    const ids = (steps: { id: string }[]) => steps.map((s) => s.id);
    expect(ids(withBilling(paid("standard")).slice(1))).toEqual(ids(build(CATALOG)));
  });
});

describe("context step", () => {
  const withContext = (contextImported?: boolean | null) =>
    buildSetupSteps({ participants: SOLO, connections: CATALOG, agentNames: new Map(), workspaceMembers: 1, contextImported });

  it("is offered last, admin-only and repeatable", () => {
    const steps = withContext(false);
    expect(steps.map((s) => s.id)).toEqual(["invite", "stripe", "github", "notion", "name-agents", "context"]);
    expect(steps.at(-1)).toMatchObject({ label: "Share your context", done: false, adminOnly: true, repeatable: true });
  });

  it("is done once the CEO holds imported context", () => {
    expect(withContext(true).find((s) => s.id === "context")?.done).toBe(true);
  });

  it("is absent when the caller does not supply the signal, which keeps mobile unchanged", () => {
    expect(withContext(undefined).some((s) => s.id === "context")).toBe(false);
    expect(withContext(null).some((s) => s.id === "context")).toBe(false);
  });
});
