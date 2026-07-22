import { renderHook, waitFor } from "@testing-library/react-native";
import { AppState } from "react-native";

// Spy created INSIDE the factory (jest hoists jest.mock above imports, so an
// outer const would be in the TDZ when the factory runs) and retrieved via
// jest.requireMock below.
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
}));
jest.mock("@isol8/shared", () => {
  const fetchJsonWithStatus = jest.fn();
  // Hand-rolled, faithful mirror of packages/shared/src/hooks/containerStatus.ts
  // (same convention as the ApiError reimplementation right below: this file
  // already avoids requireActual for @isol8/shared, so containerStatus.ts's
  // new `classifyContainerStatus` import needs a mock counterpart too, or it
  // would be `undefined` at call time). Kept in lockstep with the real
  // implementation's status-code table; packages/shared/src/hooks/
  // containerStatus.test.ts is the source of truth for that table's behavior.
  function parseBlockedPayload(body: unknown): Record<string, unknown> | null {
    const envelope =
      body && typeof body === "object" ? (body as Record<string, unknown>) : {};
    const detail = envelope.detail;
    const blocked =
      detail && typeof detail === "object" && "blocked" in (detail as object)
        ? (detail as { blocked: Record<string, unknown> }).blocked
        : (envelope.blocked as Record<string, unknown> | undefined);
    return blocked ?? null;
  }
  function classifyContainerStatus(res: { status: number; body: unknown }) {
    const { status, body } = res;
    if (status === 200) return { kind: "container", body };
    if (status === 202) {
      const detail =
        body &&
        typeof body === "object" &&
        typeof (body as Record<string, unknown>).detail === "string"
          ? (body as Record<string, unknown>).detail
          : undefined;
      return { kind: "team-pending", detail };
    }
    if (status === 404) return { kind: "needs-signup" };
    if (status === 402) {
      const blocked = parseBlockedPayload(body);
      if (!blocked) return { kind: "unrecognized", status };
      return { kind: "blocked", blocked };
    }
    return { kind: "unrecognized", status };
  }
  return {
    ApiError: class ApiError extends Error {
      status: number;
      body: unknown;
      constructor(status: number, body: unknown) {
        super("api");
        this.status = status;
        this.body = body;
      }
    },
    fetchJsonWithStatus,
    classifyContainerStatus,
    parseBlockedPayload,
  };
});
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
// `ready` (and `starting`) ContainerStatus variants, since FirstRunGate's
// funnel-eligibility decision reads it straight off this hook's status via
// ContainerStatusContext (see containerStatusContext.test.tsx).
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

// mobile onboarding-parity Slice 1: the one behavior gap this slice closes.
// The backend reports phase="provisioning" for a container whose raw
// status is "error" too, so before this slice mobile showed "starting"
// forever for a failed container. Unifies with web's ProvisioningStepper
// `container.status === "error"` check ("Setup failed" card).
describe("useContainerStatus status=error", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    setActive("active");
  });

  it("classifies a raw status of 'error' as kind 'error', not 'starting'", async () => {
    mockFetch.mockResolvedValue({
      status: 200,
      body: { status: "error", phase: "provisioning", last_error: "ECS task failed to start" },
    });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  it("classifies status='error' as 'error' even when phase is absent", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { status: "error" } });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("error"));
  });

  it("does NOT classify a non-error status as 'error' (regression guard)", async () => {
    mockFetch.mockResolvedValue({ status: 200, body: { status: "running", phase: "ready" } });
    const { result } = await renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
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
