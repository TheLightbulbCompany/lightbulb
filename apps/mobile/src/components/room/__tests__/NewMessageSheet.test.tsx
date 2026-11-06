import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { CreateRoomRequest, MemberRef } from "@lightbulb/shared";

jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: null }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "66666666-6666-4666-8666-666666666666" }));

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed —
// babel-plugin-jest-hoist rejects the rest — and every reference INSIDE a
// factory is lazy: babel lowers these `const`s to `var`, so a factory that
// captured the binding directly would see `undefined`.
interface Activation {
  activate: boolean;
}
const mockSelectDm = jest.fn(
  async (_ref: MemberRef, _options?: Activation): Promise<string | null> => "room-dm",
);
const mockCreateRoom = jest.fn(async (_request: CreateRoomRequest, _options?: Activation) => ({
  roomId: "room-group",
}));
jest.mock("../../../lib/rooms", () => ({
  useMobileRooms: () => ({
    selectDm: (ref: MemberRef, options?: Activation) => mockSelectDm(ref, options),
    createRoom: (request: CreateRoomRequest, options?: Activation) =>
      mockCreateRoom(request, options),
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
jest.mock("../../../providers/live-events", () => ({ useSuperAgent: () => false,
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
    // Reset every test to the two-person default roster — the cap test below
    // overrides this for its own run, and without a reset here that override
    // would leak into whichever test runs next.
    mockFetchMembers.mockImplementation(async () => [
      { userId: "user-a", name: "Alice" },
      { userId: "user-b", name: "Bob" },
    ]);
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
    await fireEvent.press(screen.getByLabelText("Start chat"));
    // `activate: false` is load-bearing, not decoration: this sheet stays
    // mounted behind the room screen it navigates to, and a hook instance left
    // holding that room polls `GET /rooms/{id}` every 60s and reconciles its
    // history for the rest of the session.
    await waitFor(() =>
      expect(mockSelectDm).toHaveBeenCalledWith(
        { kind: "agent", id: "agent-1" },
        { activate: false },
      ),
    );
    await waitFor(() => expect(onOpenRoom).toHaveBeenCalledWith("room-dm"));
    expect(mockCreateRoom).not.toHaveBeenCalled();
  });

  it("creates a group DM for two or more picks", async () => {
    const onOpenRoom = jest.fn();
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={onOpenRoom} />);
    await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
    await fireEvent.press(screen.getByText("Bob"));
    await fireEvent.press(screen.getByText("Friday"));
    await fireEvent.press(screen.getByLabelText("Start chat"));
    await waitFor(() =>
      expect(mockCreateRoom).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "group-dm",
          members: [
            { kind: "human", id: "user-b", role: "member", listenMode: null },
            { kind: "agent", id: "agent-1", role: "member", listenMode: "active" },
          ],
        }),
        // Same reason as the single-pick path above.
        { activate: false },
      ),
    );
    await waitFor(() => expect(onOpenRoom).toHaveBeenCalledWith("room-group"));
    expect(mockSelectDm).not.toHaveBeenCalled();
  });

  it("disables the action until at least one candidate is picked", async () => {
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
    await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
    expect(screen.getByLabelText("Start chat").props.accessibilityState.disabled).toBe(true);
    // Picking then un-picking puts it back — the row is a toggle, not a latch.
    await fireEvent.press(screen.getByText("Bob"));
    expect(screen.getByLabelText("Start chat").props.accessibilityState.disabled).toBe(false);
    await fireEvent.press(screen.getByText("Bob"));
    expect(screen.getByLabelText("Start chat").props.accessibilityState.disabled).toBe(true);
  });

  it("filters candidates by the search field", async () => {
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
    await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText("Search people and agents"), "fri");
    expect(screen.getByText("Friday")).toBeTruthy();
    expect(screen.queryByText("Bob")).toBeNull();
  });

  // Mirrors the web dialog's own participant cap (`RoomDirectory.tsx`'s
  // `canCreate`'s `selected.size <= 31` and its checkbox's
  // `disabled={!selection && selected.size >= 31}`), which itself mirrors
  // the backend's `MAX_ROOM_MEMBERS = 32` (31 picked here + the creator).
  // Applies regardless of mode; exercised here in the sheet's default
  // "direct" mode.
  it(
    "caps member selection at 31 and never lets a 32nd pick land",
    async () => {
      const many = Array.from({ length: 32 }, (_, index) => ({
        userId: `user-${index}`,
        name: `Person ${index}`,
      }));
      mockFetchMembers.mockImplementation(async () => many);
      await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
      await waitFor(() => expect(screen.getByText("Person 0")).toBeTruthy());

      for (let index = 0; index < 31; index += 1) {
        await fireEvent.press(screen.getByText(`Person ${index}`));
      }
      expect(screen.getByLabelText("Start chat").props.accessibilityState.disabled).toBe(
        false,
      );

      // Person 31 would be the 32nd pick — the row is disabled past the cap,
      // so this press is a no-op.
      await fireEvent.press(screen.getByText("Person 31"));
      await fireEvent.press(screen.getByLabelText("Start chat"));
      await waitFor(() => expect(mockCreateRoom).toHaveBeenCalled());
      const members = mockCreateRoom.mock.calls[0][0].members as { id: string }[];
      expect(members).toHaveLength(31);
      expect(members.some((member) => member.id === "user-31")).toBe(false);
    },
    // 31 sequential presses each re-render the whole sheet, and BottomSheet's
    // gesture-handler tree (GestureHandlerRootView + GestureDetector, added
    // for real pan-to-dismiss) costs more per render than the old bare
    // Animated.View did — comfortably under 5s alone, but this loop's 31
    // renders add up past jest's default per-test timeout.
    15000,
  );

  it("says so when the conversation cannot be opened", async () => {
    const onOpenRoom = jest.fn();
    mockSelectDm.mockImplementation(async () => {
      throw new Error("offline");
    });
    await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={onOpenRoom} />);
    await waitFor(() => expect(screen.getByText("Friday")).toBeTruthy());
    await fireEvent.press(screen.getByText("Friday"));
    await fireEvent.press(screen.getByLabelText("Start chat"));
    await waitFor(() => expect(screen.getByText("offline")).toBeTruthy());
    expect(onOpenRoom).not.toHaveBeenCalled();
  });

  describe("channel mode", () => {
    it("creates a channel with a trimmed name and the picked members", async () => {
      const onOpenRoom = jest.fn();
      await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={onOpenRoom} />);
      await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
      await fireEvent.press(screen.getByText("Channel"));
      await fireEvent.changeText(screen.getByLabelText("Channel name"), "  launch-room  ");
      await fireEvent.press(screen.getByText("Bob"));
      await fireEvent.press(screen.getByLabelText("Start chat"));
      await waitFor(() =>
        expect(mockCreateRoom).toHaveBeenCalledWith(
          expect.objectContaining({
            kind: "channel",
            name: "launch-room",
            members: [{ kind: "human", id: "user-b", role: "member", listenMode: null }],
          }),
          { activate: false },
        ),
      );
      await waitFor(() => expect(onOpenRoom).toHaveBeenCalledWith("room-group"));
      expect(mockSelectDm).not.toHaveBeenCalled();
    });

    it("starts a channel's agents Listening, on every plan", async () => {
      const listenMode = "active";
      await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
      await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
      await fireEvent.press(screen.getByText("Channel"));
      await fireEvent.changeText(screen.getByLabelText("Channel name"), "launch");
      await fireEvent.press(screen.getByText("Friday"));
      await fireEvent.press(screen.getByLabelText("Start chat"));
      await waitFor(() =>
        expect(mockCreateRoom).toHaveBeenCalledWith(
          expect.objectContaining({ members: [{ kind: "agent", id: "agent-1", role: "member", listenMode }] }),
          { activate: false },
        ),
      );
    });

    it("allows creating a channel with nobody picked yet", async () => {
      await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
      await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
      await fireEvent.press(screen.getByText("Channel"));
      expect(screen.getByLabelText("Start chat").props.accessibilityState.disabled).toBe(
        true,
      );
      await fireEvent.changeText(screen.getByLabelText("Channel name"), "general-2");
      expect(screen.getByLabelText("Start chat").props.accessibilityState.disabled).toBe(
        false,
      );
    });

    // Mirrors `RoomDirectory.tsx`'s channel `validName`: 1..80 trimmed characters.
    it("rejects a blank or over-limit channel name", async () => {
      await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
      await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
      await fireEvent.press(screen.getByText("Channel"));
      const nameField = screen.getByLabelText("Channel name");
      const submit = () => screen.getByLabelText("Start chat");

      await fireEvent.changeText(nameField, "   ");
      expect(submit().props.accessibilityState.disabled).toBe(true);

      await fireEvent.changeText(nameField, "a".repeat(81));
      expect(submit().props.accessibilityState.disabled).toBe(true);

      await fireEvent.changeText(nameField, "a".repeat(80));
      expect(submit().props.accessibilityState.disabled).toBe(false);
    });

    it("clears previously picked members when switching modes", async () => {
      await render(<NewMessageSheet visible onClose={() => {}} onOpenRoom={() => {}} />);
      await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
      await fireEvent.press(screen.getByText("Bob"));
      expect(screen.getByLabelText("Start chat").props.accessibilityState.disabled).toBe(
        false,
      );
      await fireEvent.press(screen.getByText("Channel"));
      await fireEvent.press(screen.getByText("Direct message"));
      // Bob's pick did not survive the round trip through channel mode, the
      // same way the web dialog clears `selected` on its kind-select onChange.
      expect(screen.getByLabelText("Start chat").props.accessibilityState.disabled).toBe(
        true,
      );
    });
  });
});
