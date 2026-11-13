import { act, render, screen } from "@testing-library/react-native";

const mockVoiceHome = jest.fn();
jest.mock("../../../../components/voice/VoiceHome", () => ({
  VoiceHome: (props: unknown) => { mockVoiceHome(props); return null; },
}));
jest.mock("../../../../components/billing/TrialRecapCard", () => ({ TrialRecapCard: () => null }));
jest.mock("../../../../components/ui/FocusedStatusBar", () => ({ FocusedStatusBar: () => null }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "room-uuid" }));
const mockRooms = [{ roomId: "r1" }];
const mockCreateRoom = jest.fn(async () => ({ roomId: "new-room" }));
jest.mock("../../../../lib/rooms", () => ({ useMobileRooms: () => ({ rooms: mockRooms, createRoom: mockCreateRoom }) }));
const mockAgents = [{ id: "ceo", role: "ceo", name: "CEO", reportsTo: null, origin: "Starter" }];
jest.mock("../../../../lib/workspaceRoster", () => ({ useWorkspaceRoster: () => ({ agents: mockAgents }) }));

import Home from "../chats";

type Props = { rooms: unknown; agentId?: string; onNewChat?: () => Promise<string> };
const props = () => mockVoiceHome.mock.calls.at(-1)![0] as Props;

it("is Voice Home only: the orb with the workspace rooms and the front agent", async () => {
  await render(<Home />);
  expect(screen.getByTestId("voice-home")).toBeTruthy();
  expect(props()).toMatchObject({ rooms: mockRooms, agentId: "ceo" });
});

it("starts a new chat as a group chat seating only the assistant", async () => {
  await render(<Home />);
  let roomId = "";
  await act(async () => { roomId = await props().onNewChat!(); });
  expect(roomId).toBe("new-room");
  expect(mockCreateRoom).toHaveBeenCalledWith({
    clientRoomId: "room-uuid", kind: "group-dm", name: null,
    members: [{ kind: "agent", id: "ceo", role: "member", listenMode: "active" }],
  }, { activate: false });
});
