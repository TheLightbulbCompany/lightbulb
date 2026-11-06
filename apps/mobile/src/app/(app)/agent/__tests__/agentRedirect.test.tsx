import { fireEvent, render, waitFor } from "@testing-library/react-native";

const mockReplace = jest.fn();
jest.mock("expo-router", () => ({
  // Lazy on purpose: babel lowers `const mockReplace` to `var`, and this factory
  // runs while the screen's import is still resolving — before it is assigned.
  router: { replace: (...args: unknown[]) => mockReplace(...args) },
  useLocalSearchParams: () => ({ agentId: "agent-1" }),
}));
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ userId: "user-a" }),
  useOrganization: () => ({ organization: null }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "44444444-4444-4444-8444-444444444444" }));

const mockSelectAgentDm = jest.fn(async (_agentId: string): Promise<string | null> => "room-9");
// The hook's failure surface: the screen reads it on each render, so tests
// flip it between renders to simulate the reject → error-card transition.
const mockRoomsState: { selectRoomError: string | null } = { selectRoomError: null };
jest.mock("../../../../lib/rooms", () => ({
  useMobileRooms: () => ({ selectAgentDm: mockSelectAgentDm, roomsLoading: false, selectRoomError: mockRoomsState.selectRoomError }),
}));

import AgentRedirect from "../[agentId]";

beforeEach(() => {
  mockReplace.mockClear();
  mockSelectAgentDm.mockClear();
  mockRoomsState.selectRoomError = null;
});

it("resolves the agent's DM room and replaces the route with it", async () => {
  await render(<AgentRedirect />);
  await waitFor(() => expect(mockSelectAgentDm).toHaveBeenCalledWith("agent-1"));
  await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/(app)/room/room-9"));
});

it("falls back to the roster instead of spinning forever when no room resolves", async () => {
  mockSelectAgentDm.mockResolvedValueOnce(null);
  await render(<AgentRedirect />);
  await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/(app)/(tabs)/chats"));
});

it("surfaces the failure with a retry instead of silently leaving", async () => {
  mockSelectAgentDm.mockRejectedValueOnce(new Error("offline"));
  const screen = await render(<AgentRedirect />);
  await waitFor(() => expect(mockSelectAgentDm).toHaveBeenCalledWith("agent-1"));
  // The hook records the failure on selectRoomError; the next render is the card.
  mockRoomsState.selectRoomError = "Conversation unavailable";
  screen.rerender(<AgentRedirect />);
  expect(await screen.findByText("Conversation unavailable")).toBeTruthy();
  expect(mockReplace).not.toHaveBeenCalled();
  fireEvent.press(screen.getByLabelText("Retry opening chat"));
  await waitFor(() => expect(mockSelectAgentDm).toHaveBeenCalledTimes(2));
});
