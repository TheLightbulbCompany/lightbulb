import { act, renderHook, waitFor } from "@testing-library/react-native";
import { AppState } from "react-native";

// Spy created INSIDE the factory (jest hoists jest.mock above imports, so an
// outer const would be in the TDZ when the factory runs) and retrieved via
// jest.requireMock below.
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
}));
// Exercise the REAL shared module — the actual `classifyContainerStatus` and
// the actual `ApiError` — and stub ONLY the network seam. A hand-rolled copy of
// the classifier here would defeat the point of having extracted one shared
// classifier: mobile would be testing a mirror that can silently drift from the
// implementation it is supposed to be sharing. This is also the dominant
// convention in this app (12 of the 15 mobile suites that mock @isol8/shared
// already spread requireActual).
jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  fetchJsonWithStatus: jest.fn(),
}));
jest.mock("../adapter", () => ({ makeAdapter: () => ({}) }));

import { useContainerStatus } from "../containerStatus";

const { fetchJsonWithStatus: mockFetch, ApiError: MockApiError } = jest.requireMock(
  "@isol8/shared",
) as {
  fetchJsonWithStatus: jest.Mock;
  ApiError: new (status: number, body: unknown) => Error;
};

function setActive(state: string) {
  (AppState as unknown as { currentState: string }).currentState = state;
}

describe("useContainerStatus parked + wake param", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setActive("active");
  });

  it("classifies phase=parked as a distinct 'parked' kind (not starting)", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { status: "stopped", phase: "parked" } });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("parked"));
  });

  it("sends wake=1 when foregrounded (present)", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { phase: "ready" } });
    await renderHook(() => useContainerStatus());
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    expect(String(mockFetch.mock.calls[0][1])).toContain("wake=1");
  });

  it("sends wake=0 when backgrounded", async () => {
    setActive("background");
    mockFetch.mockResolvedValue({ status: 200, body: { phase: "ready" } });
    await renderHook(() => useContainerStatus());
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    expect(String(mockFetch.mock.calls[0][1])).toContain("wake=0");
  });

  it("still treats phase=ready as ready", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { phase: "ready" } });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
  });
});

// mobile onboarding-parity Slice 2: `onboarded` used to be discarded when
// parsing the /container/status body. These prove it now surfaces on the
// `ready` (and `starting`) ContainerStatus variants — FirstRunGate's
// funnel-eligibility decision reads the READY one straight off this hook's
// status via ContainerStatusContext (see containerStatusContext.test.tsx).
// The `starting` case below is the parse staying faithful to the body, not a
// second eligibility signal: FirstRunGate deliberately ignores it so the
// funnel can't open behind a container the stepper may still take over.
describe("useContainerStatus onboarded field", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setActive("active");
  });

  it("surfaces onboarded:false on the ready status", async () => {
    mockFetch.mockResolvedValue({
      status: 200,
      body: { phase: "ready", onboarded: false },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.onboarded).toBe(false);
  });

  it("surfaces onboarded:true on the ready status", async () => {
    mockFetch.mockResolvedValue({
      status: 200,
      body: { phase: "ready", onboarded: true },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.onboarded).toBe(true);
  });

  it("surfaces onboarded:true on the implicit-ready status (phase absent, old backend)", async () => {
    mockFetch.mockResolvedValue({
      status: 200,
      body: { onboarded: true },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.onboarded).toBe(true);
  });

  it("leaves onboarded undefined when the field is absent from the body (old backend, no false negative)", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { phase: "ready" } });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.onboarded).toBeUndefined();
  });

  it("also surfaces onboarded on the starting status", async () => {
    mockFetch.mockResolvedValue({
      status: 200,
      body: { phase: "agent", onboarded: false },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("starting"));
    const status = result.current.status;
    if (status.kind !== "starting") throw new Error(`expected starting, got ${status.kind}`);
    expect(status.onboarded).toBe(false);
  });
});

// PR3 honest-contract consumption (INVERTED per Codex review). A
// `status:"error"` row is TERMINAL by default — it earns web's "Setup failed"
// interstitial — UNLESS its substatus is the one explicit auto-retry signal,
// "error_retrying" (backend still standing the container back up), which stays
// `starting`. LANDMINE: `phase` resolves to "provisioning" for a dead
// container too, so terminal-ness is read from substatus, NEVER phase —
// otherwise a dead container renders as an endless stepper. Keying terminal off
// `provision_failed` ALONE (the earlier mistake) silently downgraded
// deployment_failed and bare-error rows to a perpetual spinner; the catch-all
// restores their coverage.
describe("useContainerStatus status=error", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setActive("active");
  });

  it("classifies a provision_failed error as terminal kind 'error'", async () => {
    mockFetch.mockResolvedValue({
      status: 200,
      body: {
        status: "error",
        substatus: "provision_failed",
        phase: "provisioning",
        last_error: "ECS task failed to start",
      },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  // NEW: the ECS rollout circuit breaker. The provision_failed-only check
  // silently downgraded this to a perpetual "starting" spinner.
  it("classifies a deployment_failed error as terminal kind 'error' (ECS rollout circuit breaker)", async () => {
    // Also proves terminal-ness is read from substatus, not phase — the body
    // still reports phase="provisioning" for this dead container.
    mockFetch.mockResolvedValue({
      status: 200,
      body: { status: "error", substatus: "deployment_failed", phase: "provisioning" },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  // NEW (reverses the earlier "untagged → starting" mistake): when auto-retry
  // is gated / standing down the backend emits a bare error with no substatus.
  // That is a real, terminal failure — it must show the interstitial, not spin.
  it("classifies a bare error with no substatus as terminal kind 'error'", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { status: "error" } });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  // NEW: any unknown substatus is terminal too — the catch-all is what makes
  // this robust against future error shapes the app hasn't been taught.
  it("classifies an unknown error substatus as terminal kind 'error'", async () => {
    mockFetch.mockResolvedValue({
      status: 200,
      body: { status: "error", substatus: "something_unrecognized" },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  it("treats error_retrying as 'starting' — the ONE non-terminal error (keep progress + poll)", async () => {
    // The backend is auto-retrying; keep showing progress (the stepper), never
    // a terminal "Setup failed" card. substatus rides through so the surface
    // can optionally note "retrying".
    mockFetch.mockResolvedValue({
      status: 200,
      body: { status: "error", substatus: "error_retrying", phase: "provisioning" },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("starting"));
    const status = result.current.status;
    if (status.kind !== "starting") throw new Error(`expected starting, got ${status.kind}`);
    expect(status.substatus).toBe("error_retrying");
  });

  it("does NOT classify a non-error status as 'error' (regression guard)", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { status: "running", phase: "ready" } });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
  });
});

// PR3 honest-contract: top-level `teams_status` (Paperclip seeding health)
// rides through on the ready/starting variants like `onboarded`, so
// ProvisionGate can render its non-blocking Teams-degraded affordance.
describe("useContainerStatus teams_status field", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setActive("active");
  });

  it("surfaces teams_status='failed' on the ready status", async () => {
    mockFetch.mockResolvedValue({
      status: 200,
      body: { phase: "ready", teams_status: "failed" },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.teamsStatus).toBe("failed");
  });

  it("leaves teamsStatus undefined when teams_status is absent (old backend, no false alarm)", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { phase: "ready" } });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
    const status = result.current.status;
    if (status.kind !== "ready") throw new Error(`expected ready, got ${status.kind}`);
    expect(status.teamsStatus).toBeUndefined();
  });
});

// mobile onboarding-parity Slice 1: coverage for the classification paths
// this file's pre-Slice-1 version exercised only indirectly (via
// ProvisionGate.test.tsx mocking useContainerStatus outright). Now that
// classification runs through the shared classifier, these pin the
// rewired useContainerStatus's OWN mapping of each ClassifiedStatus kind.
describe("useContainerStatus blocked / needsSignup / teamPending / unreachable", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setActive("active");
  });

  it("maps a 402 with a top-level blocked payload (no detail envelope) to kind 'blocked'", async () => {
    // This is the fallback mobile's pre-Slice-1 hand-rolled parse had
    // silently dropped (it only ever read err.body.detail.blocked) —
    // unifying on the shared classifier is what restores it here.
    mockFetch.mockRejectedValue(
      new MockApiError(402, {
        blocked: {
          code: "credits_required",
          title: "Top up Claude credits",
          message: "Top up some Claude credits to start your Bedrock container.",
          action: { kind: "link", label: "Top up now", href: "/settings/billing#credits", admin_only: false },
          owner_role: "admin",
        },
      }),
    );
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("blocked"));
    const status = result.current.status;
    if (status.kind !== "blocked") throw new Error(`expected blocked, got ${status.kind}`);
    expect(status.code).toBe("credits_required");
    expect(status.href).toBe("/settings/billing#credits");
    expect(status.ownerRole).toBe("admin");
  });

  it("maps a 404 to kind 'needsSignup'", async () => {
    mockFetch.mockRejectedValue(new MockApiError(404, { detail: "No container found" }));
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("needsSignup"));
  });

  it("maps a 202 to kind 'teamPending'", async () => {
    mockFetch.mockResolvedValue({ status: 202, body: { detail: "team workspace pending" } });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("teamPending"));
  });

  it("maps an unrecognized status (e.g. 500) to kind 'unreachable'", async () => {
    mockFetch.mockRejectedValue(new MockApiError(500, { detail: "boom" }));
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("unreachable"));
  });

  it("maps a 402 with no recognizable blocked payload to kind 'unreachable' (defensive; not a real backend shape)", async () => {
    mockFetch.mockRejectedValue(new MockApiError(402, { detail: "not a gate payload" }));
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("unreachable"));
  });
});

// WHICH kinds keep polling — the behavior nothing covered until the
// whole-branch review found `error` had been given a surface but no cadence.
// Fake timers throughout: the cadence is the assertion, so the interval has
// to be driven deterministically rather than waited on.
describe("useContainerStatus poll cadence", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setActive("active");
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  /**
   * Mount, wait for the initial probe to land on `expectedKind`, and hand back
   * the fetch count so far as the baseline. waitFor is fake-timer aware (it
   * advances by its 50ms interval per iteration), so settling here costs a
   * couple hundred ms of fake clock at most — nowhere near the 5s/30s cadences
   * the cases below measure.
   */
  async function mountSettled(expectedKind: string) {
    const rendered = await renderHook(() => useContainerStatus());
    await waitFor(() =>
      expect(rendered.result.current.status.kind).toBe(expectedKind),
    );
    return { ...rendered, initialCalls: mockFetch.mock.calls.length };
  }

  // The terminal-error kind keeps its fast cadence (unchanged from when it was
  // the auto-retry trigger). Even a `provision_failed` box re-probes: an admin
  // reprovision (or a backend recovery) then clears the interstitial on its
  // own instead of stranding the user until they tap Retry. The transient
  // auto-retry case is now `starting` (see the status=error suite) and is
  // covered by the starting-cadence test below.
  it("keeps polling a terminal (provision_failed) container at the fast cadence so a re-provision recovers", async () => {
    mockFetch.mockResolvedValue({
      status: 200,
      body: { status: "error", substatus: "provision_failed" },
    });
    const { initialCalls } = await mountSettled("error");

    await act(async () => {
      jest.advanceTimersByTime(5_000);
    });
    expect(mockFetch).toHaveBeenCalledTimes(initialCalls + 1);

    await act(async () => {
      jest.advanceTimersByTime(5_000);
    });
    expect(mockFetch).toHaveBeenCalledTimes(initialCalls + 2);
  });

  it("keeps polling a starting container at the fast cadence", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { phase: "provisioning" } });
    const { initialCalls } = await mountSettled("starting");

    await act(async () => {
      jest.advanceTimersByTime(5_000);
    });
    expect(mockFetch).toHaveBeenCalledTimes(initialCalls + 1);
  });

  // A gate can clear entirely server-side (an admin finishes the ChatGPT
  // device-code flow on the web, credits land, a subscription reactivates),
  // and ChatGPTConnectScreen's terminal state has no Retry at all — so a
  // blocked status that never re-probed could strand a user on a screen whose
  // only other control is Sign out. Slow cadence, though: nothing about a
  // gate is urgent.
  it("polls a blocked gate on the slow cadence, not the fast one", async () => {
    mockFetch.mockRejectedValue(
      new MockApiError(402, {
        blocked: { code: "oauth_required", title: "Sign in with ChatGPT" },
      }),
    );
    const { initialCalls } = await mountSettled("blocked");

    await act(async () => {
      jest.advanceTimersByTime(5_000);
    });
    expect(mockFetch).toHaveBeenCalledTimes(initialCalls);

    await act(async () => {
      jest.advanceTimersByTime(25_000);
    });
    expect(mockFetch).toHaveBeenCalledTimes(initialCalls + 1);
  });

  it("stops polling once ready — a settled container is not a poll loop", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { phase: "ready" } });
    const { initialCalls } = await mountSettled("ready");

    await act(async () => {
      jest.advanceTimersByTime(120_000);
    });
    expect(mockFetch).toHaveBeenCalledTimes(initialCalls);
  });
});
