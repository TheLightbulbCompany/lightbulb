import { render, waitFor } from "@testing-library/react-native";

const mockReplace = jest.fn();
jest.mock("expo-router", () => ({
  // Lazy on purpose: babel lowers `const mockReplace` to `var`, and this factory
  // runs while the screen's import is still resolving — before it is assigned.
  router: { replace: (...args: unknown[]) => mockReplace(...args) },
  useLocalSearchParams: () => ({ agentId: "agent-1" }),
}));
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a" }),
  useOrganization: () => ({ organization: null }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "44444444-4444-4444-8444-444444444444" }));

const mockSelectAgentDm = jest.fn(async () => "room-9");
jest.mock("../../../../lib/rooms", () => ({
  useMobileRooms: () => ({ selectAgentDm: mockSelectAgentDm, roomsLoading: false }),
}));

import AgentRedirect from "../[agentId]";

it("resolves the agent's DM room and replaces the route with it", async () => {
  await render(<AgentRedirect />);
  await waitFor(() => expect(mockSelectAgentDm).toHaveBeenCalledWith("agent-1"));
  await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/(app)/room/room-9"));
});
