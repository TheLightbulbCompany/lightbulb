import { render, waitFor } from "@testing-library/react-native";

const mockReplace = jest.fn();
const mockPush = jest.fn();
jest.mock("expo-router", () => ({
  Redirect: ({ href }: { href: string }) => { mockReplace(href); return null; },
  useRouter: () => ({ replace: (h: string) => mockReplace(h), push: (h: string) => mockPush(h) }),
}));
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ userId: "user-a" }) }));
const mockAgents: { agents: { id: string; role?: string }[] } = { agents: [{ id: "ceo-1", role: "ceo" }] };
jest.mock("@lightbulb/shared", () => ({ useAgents: () => ({ agents: mockAgents.agents, isLoading: false }) }));
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => null }));
const mockRooms: { rooms: unknown[] } = { rooms: [] };
const mockSelectAgentDm = jest.fn(async (_id: string): Promise<string | null> => "room-new");
jest.mock("../../../lib/rooms", () => ({
  useMobileRooms: () => ({ rooms: mockRooms.rooms, roomsLoading: false, selectAgentDm: mockSelectAgentDm }),
}));

import PhoneHome from "../index";

beforeEach(() => {
  mockReplace.mockClear(); mockPush.mockClear(); mockSelectAgentDm.mockClear();
  mockRooms.rooms = []; mockAgents.agents = [{ id: "ceo-1", role: "ceo" }];
});

it("opens the existing CEO DM on top of Chats, so Back reaches the tabs", async () => {
  mockRooms.rooms = [{ roomId: "room-dm", kind: "dm", members: [{ id: "user-a" }, { id: "ceo-1" }] }];
  await render(<PhoneHome />);
  await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-dm"));
  expect(mockReplace).toHaveBeenCalledWith("/(app)/(tabs)/chats");
  expect(mockSelectAgentDm).not.toHaveBeenCalled();
});

it("creates the CEO DM for a member who has none yet", async () => {
  await render(<PhoneHome />);
  await waitFor(() => expect(mockSelectAgentDm).toHaveBeenCalledWith("ceo-1"));
  await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-new"));
});

it("falls back to Chats when the DM cannot be opened", async () => {
  mockSelectAgentDm.mockRejectedValueOnce(new Error("offline"));
  await render(<PhoneHome />);
  await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/(app)/(tabs)/chats"));
  expect(mockPush).not.toHaveBeenCalled();
});

it("goes to Chats in a workspace with no CEO", async () => {
  mockAgents.agents = [{ id: "a-1", role: "engineer" }];
  await render(<PhoneHome />);
  await waitFor(() => expect(mockReplace).toHaveBeenCalledWith("/(app)/(tabs)/chats"));
  expect(mockSelectAgentDm).not.toHaveBeenCalled();
});
