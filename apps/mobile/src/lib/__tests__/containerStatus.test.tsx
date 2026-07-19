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
