import { act, renderHook, waitFor } from "@testing-library/react-native";
import { AppState } from "react-native";

/**
 * `useContainerStatus` is now a PURE READER over the ONE shared
 * `useOnboardingStatus` hook (spec PR-9, CLIENT-HOOK) — the same hook web's
 * `useProvisioningState` delegates to. This file therefore mocks
 * `useOnboardingStatus` itself (not the network seam) and tests ONLY what
 * this file still owns: mapping a `ClassifiedStatus` into this file's richer
 * `ContainerStatus`, and wiring `wake`/`visibility` correctly. The shared
 * hook's own fetch/cadence/backstop-loop/AbortController machinery is tested
 * once, at the source, in
 * packages/shared/src/hooks/useOnboardingStatus.test.ts — re-mocking
 * `fetchJsonWithStatus` here (the pre-CLIENT-HOOK strategy) no longer
 * intercepts anything: the shared hook imports it from its own internal
 * `../api/client`, not the `@lightbulb/shared` barrel this file's mock used to
 * patch.
 */
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
}));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useOnboardingStatus: jest.fn(),
}));
jest.mock("../adapter", () => ({ makeAdapter: () => ({}) }));

import { useContainerStatus } from "../containerStatus";

const { useOnboardingStatus: mockUseOnboardingStatus } = jest.requireMock(
  "@lightbulb/shared",
) as {
  useOnboardingStatus: jest.Mock;
};

function setActive(state: string) {
  (AppState as unknown as { currentState: string }).currentState = state;
}

/** Shapes a mock return value the way the real `useOnboardingStatus` would
 * for a classified 200 "container" body. */
function containerResult(body: Record<string, unknown>, refresh: jest.Mock = jest.fn()) {
  return {
    classified: { kind: "container" as const, body },
    derived: null,
    networkError: undefined,
    isLoading: false,
    refresh,
  };
}

function loadingResult() {
  return {
    classified: null,
    derived: null,
    networkError: undefined,
    isLoading: true,
    refresh: jest.fn(),
  };
}

function networkErrorResult(message: string) {
  return {
    classified: null,
    derived: null,
    networkError: new Error(message),
    isLoading: false,
    refresh: jest.fn(),
  };
}

describe("useContainerStatus parked + visibility wiring", () => {
  beforeEach(() => {
    mockUseOnboardingStatus.mockReset();
    setActive("active");
  });

  it("classifies runtime_phase-derived lifecycle=parked as a distinct 'parked' kind (not starting)", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ container_status: "stopped", lifecycle: "parked", runtime_phase: "provisioning", parked: true }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("parked"));
  });

  it("sends no `wake` option (the status read is cold)", async () => {
    mockUseOnboardingStatus.mockReturnValue(containerResult({ runtime_phase: "ready", lifecycle: "ready" }));
    await renderHook(() => useContainerStatus());
    expect(mockUseOnboardingStatus.mock.calls[0][0]).not.toHaveProperty("wake");
  });

  it("passes a `visibility` adapter wired to AppState", async () => {
    mockUseOnboardingStatus.mockReturnValue(containerResult({ runtime_phase: "ready", lifecycle: "ready" }));
    await renderHook(() => useContainerStatus());
    const options = mockUseOnboardingStatus.mock.calls[0][0];
    setActive("active");
    expect(options.visibility.isVisible()).toBe(true);
    setActive("background");
    expect(options.visibility.isVisible()).toBe(false);
  });

  it("still treats runtime_phase=ready as ready", async () => {
    mockUseOnboardingStatus.mockReturnValue(containerResult({ runtime_phase: "ready", lifecycle: "ready" }));
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
  });
});

// mobile onboarding-parity Slice 2: `onboarded` used to be discarded when
// parsing the /container/status body. These prove it now surfaces on the
// `ready` (and `starting`) ContainerStatus variants — FirstRunGate's
// funnel-eligibility decision read the READY one straight off this hook's
// status.
describe("useContainerStatus onboarded field", () => {
  beforeEach(() => {
    mockUseOnboardingStatus.mockReset();
    setActive("active");
  });

  it("surfaces onboarded:false on the ready status", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ runtime_phase: "ready", lifecycle: "ready", onboarded: false }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.onboarded).toBe(false);
  });

  it("surfaces onboarded:true on the ready status", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ runtime_phase: "ready", lifecycle: "ready", onboarded: true }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.onboarded).toBe(true);
  });

  it("leaves onboarded undefined when the field is absent from the body (no false negative)", async () => {
    mockUseOnboardingStatus.mockReturnValue(containerResult({ runtime_phase: "ready", lifecycle: "ready" }));
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.onboarded).toBeUndefined();
  });

  it("also surfaces onboarded on the starting status", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ runtime_phase: "starting", lifecycle: "starting", onboarded: false }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("starting"));
    const status = result.current.status;
    if (status.kind !== "starting") throw new Error(`expected starting, got ${status.kind}`);
    expect(status.onboarded).toBe(false);
  });

  // L4 regression guard, mobile half — "mobile never re-onboards" dies by
  // construction now that useContainerStatus is a PURE READER with no
  // client-side latch: a wipe/reprovision that flips the backend's
  // `onboarded` back to false must be reflected immediately, on the very
  // next classified update, with no stale "already onboarded" memory
  // surviving the transition.
  it("re-onboards after a reprovision: onboarded flips true -> false on the next update with no stale latch", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ runtime_phase: "ready", lifecycle: "ready", onboarded: true }),
    );
    const { result, rerender } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const before = result.current.status;
    if (before.kind !== "ready") throw new Error(`expected ready, got ${before.kind}`);
    expect(before.onboarded).toBe(true);

    // Owner wipes + reprovisions: a fresh container/company row comes back
    // with onboarded:false on the very next poll.
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ runtime_phase: "ready", lifecycle: "ready", onboarded: false }),
    );
    await act(async () => {
      rerender(undefined);
    });

    await waitFor(() => {
      const status = result.current.status;
      if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
      expect(status.onboarded).toBe(false);
    });
  });
});

// PR3 honest-contract consumption (INVERTED per Codex review). A
// `container_status:"error"` row is TERMINAL by default — it earns web's
// "Setup failed" interstitial — UNLESS its substatus is the one explicit
// auto-retry signal, "error_retrying" (backend still standing the container
// back up), which stays `starting`. LANDMINE: `runtime_phase` resolves to
// "provisioning" for a dead container too, so terminal-ness is read from
// `container_substatus`, NEVER `runtime_phase` — otherwise a dead container
// renders as an endless stepper.
describe("useContainerStatus container_status=error", () => {
  beforeEach(() => {
    mockUseOnboardingStatus.mockReset();
    setActive("active");
  });

  it("classifies a provision_failed error as terminal kind 'error'", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({
        container_status: "error",
        container_substatus: "provision_failed",
        runtime_phase: "provisioning",
        last_error: "ECS task failed to start",
      }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  it("classifies a deployment_failed error as terminal kind 'error' (ECS rollout circuit breaker)", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({
        container_status: "error",
        container_substatus: "deployment_failed",
        runtime_phase: "provisioning",
      }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  it("classifies a bare error with no substatus as terminal kind 'error'", async () => {
    mockUseOnboardingStatus.mockReturnValue(containerResult({ container_status: "error" }));
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  it("classifies an unknown error substatus as terminal kind 'error'", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ container_status: "error", container_substatus: "something_unrecognized" }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  it("treats error_retrying as 'starting' — the ONE non-terminal error (keep progress + poll)", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({
        container_status: "error",
        container_substatus: "error_retrying",
        runtime_phase: "provisioning",
      }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("starting"));
    const status = result.current.status;
    if (status.kind !== "starting") throw new Error(`expected starting, got ${status.kind}`);
    expect(status.substatus).toBe("error_retrying");
  });

  it("does NOT classify a non-error status as 'error' (regression guard)", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ container_status: "running", runtime_phase: "ready", lifecycle: "ready" }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
  });
});

// PR3 honest-contract: the composed contract's `teams_state` (Paperclip
// seeding health) rides through on the ready/starting variants like
// `onboarded`, so ProvisionGate can render its non-blocking Teams-degraded
// affordance. No legacy `teams_status` flat duplicate anymore (spec PR-9,
// CLIENT-HOOK deleted it from `_status_response` — there are no users and no
// backward-compat requirement).
describe("useContainerStatus teams_state field", () => {
  beforeEach(() => {
    mockUseOnboardingStatus.mockReset();
    setActive("active");
  });

  it("surfaces teams_state='failed' on the ready status", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ runtime_phase: "ready", lifecycle: "ready", teams_state: "failed" }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.teamsStatus).toBe("failed");
  });

  it("leaves teamsStatus undefined when teams_state is absent (no false alarm)", async () => {
    mockUseOnboardingStatus.mockReturnValue(containerResult({ runtime_phase: "ready", lifecycle: "ready" }));
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.teamsStatus).toBeUndefined();
  });
});

// Issue #1468: Friday's server-dispatched first-run kickoff rides the contract
// as `bootstrap`, and nothing outside the admin page ever read it — so an owner
// whose first run died sat on an empty chat forever.

describe("useContainerStatus blocked / needsSignup / teamPending / unreachable", () => {
  beforeEach(() => {
    mockUseOnboardingStatus.mockReset();
    setActive("active");
  });

  it("maps a classified 'blocked' outcome to kind 'blocked'", async () => {
    mockUseOnboardingStatus.mockReturnValue({
      classified: {
        kind: "blocked" as const,
        blocked: {
          code: "credits_required",
          title: "Top up Claude credits",
          message: "Top up some Claude credits to start your Bedrock container.",
          action: { kind: "link", label: "Top up now", href: "/settings/billing#credits", admin_only: false },
          owner_role: "admin",
        },
      },
      derived: null,
      networkError: undefined,
      isLoading: false,
      refresh: jest.fn(),
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("blocked"));
    const status = result.current.status;
    if (status.kind !== "blocked") throw new Error(`expected blocked, got ${status.kind}`);
    expect(status.code).toBe("credits_required");
    expect(status.href).toBe("/settings/billing#credits");
    expect(status.ownerRole).toBe("admin");
  });

  it("maps a classified 'needs-signup' outcome to kind 'needsSignup'", async () => {
    mockUseOnboardingStatus.mockReturnValue({
      classified: { kind: "needs-signup" as const },
      derived: null,
      networkError: undefined,
      isLoading: false,
      refresh: jest.fn(),
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("needsSignup"));
  });

  it("maps a classified 'team-pending' outcome to kind 'teamPending'", async () => {
    mockUseOnboardingStatus.mockReturnValue({
      classified: { kind: "team-pending" as const, detail: "team workspace pending" },
      derived: null,
      networkError: undefined,
      isLoading: false,
      refresh: jest.fn(),
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("teamPending"));
  });

  it("maps a classified 'unrecognized' outcome (e.g. 500) to kind 'unreachable'", async () => {
    mockUseOnboardingStatus.mockReturnValue({
      classified: { kind: "unrecognized" as const, status: 500 },
      derived: null,
      networkError: undefined,
      isLoading: false,
      refresh: jest.fn(),
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("unreachable"));
  });

  it("maps a genuine network error (no classified outcome yet) to kind 'unreachable'", async () => {
    mockUseOnboardingStatus.mockReturnValue(networkErrorResult("Network request failed"));
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("unreachable"));
    const status = result.current.status;
    if (status.kind !== "unreachable") throw new Error(`expected unreachable, got ${status.kind}`);
    expect(status.message).toBe("Network request failed");
  });

  // F4: a session with no active org gets 409 `workspace_required`. That used to
  // read as `unreachable`, which never blocks, so Home dead-ended on Retry.
  it("maps a 409 (workspace_required) to the blocking 'workspaceRequired' kind", async () => {
    mockUseOnboardingStatus.mockReturnValue({
      ...networkErrorResult("Unexpected /container/status status: 409"),
      networkError: Object.assign(new Error("Unexpected /container/status status: 409"), { status: 409 }),
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("workspaceRequired"));
  });

  it("maps the initial (neither classified nor errored) state to kind 'loading'", async () => {
    mockUseOnboardingStatus.mockReturnValue(loadingResult());
    const { result } = await renderHook(() => useContainerStatus());
    expect(result.current.status.kind).toBe("loading");
  });
});

describe("useContainerStatus refetch", () => {
  beforeEach(() => {
    mockUseOnboardingStatus.mockReset();
    setActive("active");
  });

  it("refetch delegates to the shared hook's refresh()", async () => {
    const refresh = jest.fn();
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ runtime_phase: "ready", lifecycle: "ready" }, refresh),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    result.current.refetch();
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});

describe("useContainerStatus live failure vs sticky last-good", () => {
  beforeEach(() => {
    mockUseOnboardingStatus.mockReset();
    setActive("active");
  });

  // `useOnboardingStatus` keeps the last good `classified` across a failed
  // poll so web's first-run funnel isn't unmounted by a blip. That made this
  // binding's old `classified ? ... : networkError ? ...` order drop the
  // failure signal entirely once ANY good poll had landed: a sustained
  // outage rendered a frozen cold-start screen with no way out. Mobile's
  // `unreachable` is NON-BLOCKING (ProvisionGate falls through to children),
  // so reporting the live transport failure is the honest read here.
  it("reports unreachable while the current poll is failing, even with a sticky last-good body", async () => {
    mockUseOnboardingStatus.mockReturnValue({
      classified: { kind: "container" as const, body: { runtime_phase: "starting", lifecycle: "starting" } },
      derived: null,
      networkError: new Error("Failed to fetch"),
      isLoading: false,
      refresh: jest.fn(),
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("unreachable"));
  });

  it("still reports the container status once the poll recovers", async () => {
    mockUseOnboardingStatus.mockReturnValue(
      containerResult({ runtime_phase: "ready", lifecycle: "ready", onboarded: true }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
  });
});
