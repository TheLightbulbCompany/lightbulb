import { renderHook, waitFor } from "@testing-library/react-native";
import { AppState } from "react-native";

const fetchSpy = jest.fn();
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
}));
jest.mock("@isol8/shared", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    body: unknown;
    constructor(status: number, body: unknown) {
      super("api");
      this.status = status;
      this.body = body;
    }
  },
  fetchJsonWithStatus: (...args: unknown[]) => fetchSpy(...args),
}));
jest.mock("../adapter", () => ({ makeAdapter: () => ({}) }));

import { useContainerStatus } from "../containerStatus";

function setActive(state: string) {
  (AppState as unknown as { currentState: string }).currentState = state;
}

describe("useContainerStatus parked + wake param", () => {
  beforeEach(() => {
    fetchSpy.mockReset();
    setActive("active");
  });

  it("classifies phase=parked as a distinct 'parked' kind (not starting)", async () => {
    fetchSpy.mockResolvedValue({ status: 200, body: { status: "stopped", phase: "parked" } });
    const { result } = renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("parked"));
  });

  it("sends wake=1 when foregrounded (present)", async () => {
    fetchSpy.mockResolvedValue({ status: 200, body: { phase: "ready" } });
    renderHook(() => useContainerStatus());
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    expect(String(fetchSpy.mock.calls[0][1])).toContain("wake=1");
  });

  it("sends wake=0 when backgrounded", async () => {
    setActive("background");
    fetchSpy.mockResolvedValue({ status: 200, body: { phase: "ready" } });
    renderHook(() => useContainerStatus());
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    expect(String(fetchSpy.mock.calls[0][1])).toContain("wake=0");
  });

  it("still treats phase=ready as ready", async () => {
    fetchSpy.mockResolvedValue({ status: 200, body: { phase: "ready" } });
    const { result } = renderHook(() => useContainerStatus());
    await waitFor(() => expect(result.current.status.kind).toBe("ready"));
  });
});
