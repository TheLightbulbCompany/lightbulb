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
  };
});
jest.mock("../adapter", () => ({ makeAdapter: () => ({}) }));

import { useContainerStatus } from "../containerStatus";

const { fetchJsonWithStatus: mockFetch } = jest.requireMock("@isol8/shared") as {
  fetchJsonWithStatus: jest.Mock;
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
