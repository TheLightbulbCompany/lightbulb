import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { CreateRoomRequest, MemberRef } from "@lightbulb/shared";

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: null }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "66666666-6666-4666-8666-666666666666" }));

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed —
// babel-plugin-jest-hoist rejects the rest — and every reference INSIDE a
// factory is lazy: babel lowers these `const`s to `var`, so a factory that
// captured the binding directly would see `undefined`.
const mockSelectDm = jest.fn(async (_ref: MemberRef): Promise<string | null> => "room-dm");
const mockCreateRoom = jest.fn(async (_request: CreateRoomRequest) => ({ roomId: "room-group" }));
jest.mock("../../../lib/rooms", () => ({
  useMobileRooms: () => ({
    selectDm: (ref: MemberRef) => mockSelectDm(ref),
    createRoom: (request: CreateRoomRequest) => mockCreateRoom(request),
  }),
}));
const mockFetchMembers = jest.fn(async () => [
  { userId: "user-a", name: "Alice" },
  { userId: "user-b", name: "Bob" },
]);
jest.mock("../../../lib/teamsData", () => ({
  useTeamsConfig: () => ({}),
  fetchMembers: () => mockFetchMembers(),
}));
// The agent roster reads through the live-events runtime, which throws outside
// its provider.
jest.mock("../../../providers/live-events", () => ({
  useLiveEvents: () => ({
    httpReady: true,
    liveConnected: true,
    liveError: null,
    connectionRevision: 1,
    rpc: jest.fn(),
    getOperation: jest.fn(),
    onEvent: () => () => {},
  }),
}));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useAgents: () => ({
    agents: [{ id: "agent-1", name: "Friday" }],
    isLoading: false,
    isSettled: true,
  }),
}));

import { NewMessageSheet } from "../NewMessageSheet";

describe("NewMessageSheet", () => {
  beforeEach(() => {
    mockSelectDm.mockClear();
    mockCreateRoom.mockClear();
    mockSelectDm.mockImplementation(async () => "room-dm");
  });

  it("lists people and agents, and never the signed-in user", async () => {
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
    await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
    expect(screen.getByText("Friday")).toBeTruthy();
    expect(screen.queryByText("Alice")).toBeNull();
  });

  it("opens the canonical DM for a single pick", async () => {
    const onOpenRoom = jest.fn();
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={onOpenRoom} />);
    await waitFor(() => expect(screen.getByText("Friday")).toBeTruthy());
    await fireEvent.press(screen.getByText("Friday"));
    await fireEvent.press(screen.getByLabelText("Start conversation"));
    await waitFor(() => expect(mockSelectDm).toHaveBeenCalledWith({ kind: "agent", id: "agent-1" }));
    await waitFor(() => expect(onOpenRoom).toHaveBeenCalledWith("room-dm"));
    expect(mockCreateRoom).not.toHaveBeenCalled();
  });

  it("creates a group DM for two or more picks", async () => {
    const onOpenRoom = jest.fn();
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={onOpenRoom} />);
    await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
    await fireEvent.press(screen.getByText("Bob"));
    await fireEvent.press(screen.getByText("Friday"));
    await fireEvent.press(screen.getByLabelText("Start conversation"));
    await waitFor(() =>
      expect(mockCreateRoom).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "group-dm",
          members: [
            { kind: "human", id: "user-b", role: "member", listenMode: null },
            { kind: "agent", id: "agent-1", role: "member", listenMode: "active" },
          ],
        }),
      ),
    );
    await waitFor(() => expect(onOpenRoom).toHaveBeenCalledWith("room-group"));
    expect(mockSelectDm).not.toHaveBeenCalled();
  });

  it("disables the action until at least one candidate is picked", async () => {
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
    await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
    expect(screen.getByLabelText("Start conversation").props.accessibilityState.disabled).toBe(true);
    // Picking then un-picking puts it back — the row is a toggle, not a latch.
    await fireEvent.press(screen.getByText("Bob"));
    expect(screen.getByLabelText("Start conversation").props.accessibilityState.disabled).toBe(false);
    await fireEvent.press(screen.getByText("Bob"));
    expect(screen.getByLabelText("Start conversation").props.accessibilityState.disabled).toBe(true);
  });

  it("filters candidates by the search field", async () => {
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
    await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText("Search people and agents"), "fri");
    expect(screen.getByText("Friday")).toBeTruthy();
    expect(screen.queryByText("Bob")).toBeNull();
  });

  it("says so when the conversation cannot be opened", async () => {
    const onOpenRoom = jest.fn();
    mockSelectDm.mockImplementation(async () => {
      throw new Error("offline");
    });
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={onOpenRoom} />);
    await waitFor(() => expect(screen.getByText("Friday")).toBeTruthy());
    await fireEvent.press(screen.getByText("Friday"));
    await fireEvent.press(screen.getByLabelText("Start conversation"));
    await waitFor(() => expect(screen.getByText("offline")).toBeTruthy());
    expect(onOpenRoom).not.toHaveBeenCalled();
  });
});
