import { describe, expect, it } from "vitest";

import { deriveUIPhase, type StatusContract } from "./statusContract";

/**
 * One implementation, one test file — web and mobile both call
 * `deriveUIPhase` directly (no per-platform wrapper reimplements this
 * logic), so these scenarios prove every lifecycle renders correctly on
 * BOTH surfaces simultaneously. See ProvisioningStepper.tsx (web) and
 * mobile's containerStatus.ts (fromClassified) for the call sites.
 */

function baseContract(overrides: Partial<StatusContract> = {}): StatusContract {
  return {
    owner_id: "user_x",
    lifecycle: "ready",
    runtime_phase: "ready",
    chat_available: true,
    container_status: "running",
    container_substatus: null,
    parked: false,
    onboarded: true,
    last_error: null,
    last_error_at: null,
    service_name: "openclaw-user_x-abcd",
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-20T00:00:00Z",
    generations: {
      provision_generation: "01J000000000000000000000",
      onboarding_generation: "01J000000000000000000001",
    },
    funnel: {
      state: "complete",
      goal_id: "goal_1",
      project_id: "proj_1",
      submitted_at: "2026-07-01T00:05:00Z",
      completed_at: "2026-07-01T00:06:00Z",
    },
    bootstrap: {
      state: "delivered",
      dispatch_id: "disp_1",
      dispatched_at: "2026-07-01T00:06:01Z",
      delivered_at: "2026-07-01T00:06:30Z",
      attempts: 1,
      last_error: null,
    },
    provider: {
      choice: "bedrock_claude",
      byo_provider: null,
      is_org: false,
      subscription_status: "trial_local",
    },
    teams_state: "active",
    ...overrides,
  };
}

describe("deriveUIPhase — the seven required lifecycle scenarios", () => {
  it("blocked", () => {
    const contract = baseContract({
      lifecycle: "blocked",
      runtime_phase: "provisioning",
      chat_available: false,
      container_status: null,
    });
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("blocked");
    expect(result.chatAvailable).toBe(false);
  });

  it("provider_selection_required — org owner, active seats, no provider chosen", () => {
    const contract = baseContract({
      lifecycle: "provider_selection_required",
      runtime_phase: "provisioning",
      chat_available: false,
      container_status: null,
      provider: { choice: null, byo_provider: null, is_org: true, subscription_status: "active" },
    });
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("provider_selection_required");
    expect(result.chatAvailable).toBe(false);
  });

  it("needs_signup", () => {
    const contract = baseContract({
      lifecycle: "needs_signup",
      runtime_phase: "provisioning",
      chat_available: false,
      container_status: null,
    });
    expect(deriveUIPhase(contract).phase).toBe("needs_signup");
  });

  it("team-pending — a CALLER-ROLE fact, not a lifecycle value, so it's an explicit option", () => {
    const contract = baseContract({
      lifecycle: "needs_signup",
      runtime_phase: "provisioning",
      chat_available: false,
      container_status: null,
    });
    const result = deriveUIPhase(contract, { isTeamPending: true });
    expect(result.phase).toBe("team_pending");
    expect(result.chatAvailable).toBe(false);
  });

  it("cold-start — provisioning", () => {
    const contract = baseContract({
      lifecycle: "provisioning",
      runtime_phase: "provisioning",
      chat_available: false,
      container_status: "provisioning",
    });
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("cold_start");
    expect(result.runtimePhase).toBe("provisioning");
    expect(result.chatAvailable).toBe(false);
  });

  it("cold-start — starting (gateway not yet connected)", () => {
    const contract = baseContract({
      lifecycle: "starting",
      runtime_phase: "starting",
      chat_available: false,
      container_status: "running",
    });
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("cold_start");
    expect(result.runtimePhase).toBe("starting");
    expect(result.chatAvailable).toBe(false);
  });

  it("error", () => {
    const contract = baseContract({
      lifecycle: "error",
      runtime_phase: "provisioning",
      chat_available: false,
      container_status: "error",
      container_substatus: "provision_failed",
      last_error: "ECS task failed to start",
    });
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("error");
    expect(result.chatAvailable).toBe(false);
  });

  it("parked — deliberately-asleep trial box, one intent away from running", () => {
    const contract = baseContract({
      lifecycle: "parked",
      runtime_phase: "provisioning",
      chat_available: false,
      container_status: "stopped",
      parked: true,
    });
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("parked");
    expect(result.chatAvailable).toBe(false);
  });

  it("ready", () => {
    const contract = baseContract();
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("ready");
    expect(result.chatAvailable).toBe(true);
  });
});

describe("INV-6 — onboarding status never gates chat", () => {
  it("a not-yet-submitted funnel over a ready runtime still yields a live chat surface", () => {
    const contract = baseContract({
      funnel: {
        state: "not_started",
        goal_id: null,
        project_id: null,
        submitted_at: null,
        completed_at: null,
      },
      onboarded: false,
    });
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("ready");
    expect(result.chatAvailable).toBe(true);
  });

  it("a failed Teams (Paperclip) seed over a ready runtime still yields a live chat surface — degraded, not blocking", () => {
    const contract = baseContract({ teams_state: "failed" });
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("ready");
    expect(result.chatAvailable).toBe(true);
    expect(result.teamsDegraded).toBe(true);
  });

  it("a dispatched-but-not-yet-delivered bootstrap over a ready runtime still yields a live chat surface", () => {
    const contract = baseContract({
      bootstrap: {
        state: "dispatched",
        dispatch_id: "disp_2",
        dispatched_at: "2026-07-01T00:06:01Z",
        delivered_at: null,
        attempts: 1,
        last_error: null,
      },
    });
    const result = deriveUIPhase(contract);
    expect(result.phase).toBe("ready");
    expect(result.chatAvailable).toBe(true);
  });

  it("chatAvailable mirrors runtime_phase alone even when lifecycle disagrees (defensive: a hand-built contract should never do this in practice)", () => {
    const contract = baseContract({
      lifecycle: "blocked",
      runtime_phase: "ready",
      chat_available: true,
    });
    const result = deriveUIPhase(contract);
    expect(result.chatAvailable).toBe(true);
    expect(result.runtimePhase).toBe("ready");
  });
});

describe("deriveUIPhase — non-blocking teamsDegraded flag", () => {
  it("is false for a healthy seed", () => {
    expect(deriveUIPhase(baseContract({ teams_state: "active" })).teamsDegraded).toBe(false);
  });

  it("is false when teams_state is absent (old backend / not yet known)", () => {
    expect(deriveUIPhase(baseContract({ teams_state: null })).teamsDegraded).toBe(false);
  });
});
