import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { Alert } from "react-native";

const mockGet = jest.fn();
const mockPut = jest.fn();

jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ getToken: jest.fn(async () => "tok") }) }));
jest.mock("expo-router", () => ({
  router: { back: jest.fn() },
  Stack: { Screen: () => null },
  useLocalSearchParams: () => ({ agentId: "forge" }),
}));
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => ({}) }));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ get: mockGet, put: mockPut }),
  useAgents: () => ({ agents: [{ id: "forge", name: "Forge" }], defaultId: null, refresh: jest.fn() }),
  useStatusLoaded: () => false,
  useRuntimeRpc: () => ({ data: undefined, mutate: jest.fn() }),
  useRuntimeRpcMutation: () => jest.fn(),
}));

import AgentDetail from "../agent-detail";

const PATH = "/teams/agents/forge/voice-notes";

beforeEach(() => {
  jest.clearAllMocks();
  mockGet.mockResolvedValue({ name: "Forge", notes: "Dry wit.\nShort.", voice: "beacon" });
  mockPut.mockResolvedValue({});
});

it("shows the agent's own voice and the first line of the notes", async () => {
  await render(<AgentDetail />);
  expect(await screen.findByText("Dry wit.")).toBeTruthy();
  expect(screen.getByText("Beacon")).toBeTruthy();
  expect(mockGet).toHaveBeenCalledWith(PATH);
});

it("shows None when there are no notes", async () => {
  mockGet.mockResolvedValue({ name: "Forge", notes: "", voice: "marin" });
  await render(<AgentDetail />);
  expect(await screen.findByText("None")).toBeTruthy();
});

it("shows Unavailable and never opens the editor when the GET fails", async () => {
  mockGet.mockRejectedValue(new Error("500"));
  await render(<AgentDetail />);
  await fireEvent.press(await screen.findByText("Unavailable"));
  expect(screen.queryByLabelText("Voice notes")).toBeNull();
  expect(mockPut).not.toHaveBeenCalled();
});

it("caps the editor at 1000 and counts", async () => {
  await render(<AgentDetail />);
  await fireEvent.press(await screen.findByText("Dry wit."));
  const input = screen.getByLabelText("Voice notes");
  expect(input.props.maxLength).toBe(1000);
  expect(screen.getByText("15 / 1,000")).toBeTruthy();
});

it("saves with PUT and refetches", async () => {
  await render(<AgentDetail />);
  await fireEvent.press(await screen.findByText("Dry wit."));
  await fireEvent.changeText(screen.getByLabelText("Voice notes"), "Warm.");
  mockGet.mockResolvedValue({ name: "Forge", notes: "Warm.", voice: "marin" });
  await fireEvent.press(screen.getByText("Save"));
  await waitFor(() => expect(mockPut).toHaveBeenCalledWith(PATH, { notes: "Warm." }));
  expect(await screen.findByText("Warm.")).toBeTruthy();
});

it("alerts when the PUT fails", async () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  mockPut.mockRejectedValue(new Error("422"));
  await render(<AgentDetail />);
  await fireEvent.press(await screen.findByText("Dry wit."));
  await fireEvent.press(screen.getByText("Save"));
  await waitFor(() => expect(alert).toHaveBeenCalledWith("Couldn’t save voice notes", "422"));
});
