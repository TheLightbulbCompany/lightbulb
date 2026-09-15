import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { LocalSession } from "@lightbulb/shared";

const mockPush = jest.fn();
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: (...args: unknown[]) => mockPush(...args) },
    Stack: { Screen: () => null },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(cb, [cb]),
  };
});
jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ userId: "user-a" }) }));

// A minimal, controllable double of useMobileRooms: enough for the screen's
// resourceSource/rooms/activeRoomId/selectDm/selectRoomError reads, with a
// REAL useState behind selectRoomError so a rejected selectDm's error shows
// up the same way the real useRooms hook would (it flips selectRoomError on
// failure, which is what the screen renders).
const mockListWorkspaceSessions = jest.fn(async () => ({ sessions: [] as LocalSession[] }));
const mockResourceSource = {
  client: { listWorkspaceSessions: mockListWorkspaceSessions },
  onEvent: (_handler: (event: unknown) => void) => () => {},
  connectionRevision: 1,
};
const mockRoomsList = [{ roomId: "room-1", members: [{ kind: "human", id: "bob", displayName: "Bob" }] }];
const mockSelectDm = jest.fn(async (_participant: { kind: string; id: string }, _options?: { activate?: boolean }) => "dm-1");
jest.mock("../../../lib/rooms", () => {
  const { useState } = require("react");
  return {
    useMobileRooms: () => {
      const [selectRoomError, setSelectRoomError] = useState(null as string | null);
      return {
        rooms: mockRoomsList,
        activeRoomId: null,
        resourceSource: mockResourceSource,
        selectDm: async (participant: { kind: string; id: string }, options: { activate?: boolean }) => {
          try {
            const roomId = await mockSelectDm(participant, options);
            setSelectRoomError(null);
            return roomId;
          } catch {
            setSelectRoomError("this conversation could not be opened");
            throw new Error("this conversation could not be opened");
          }
        },
        selectRoomError,
      };
    },
  };
});

import SharedSessionsScreen from "../sessions";

const bobSession: LocalSession = {
  sessionId: "sess-bob", deviceId: "mac-1", humanUserId: "bob", provider: "claude", providerVersion: "2", generation: "g",
  status: "idle", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false },
  capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, title: "Fix bug",
};

describe("shared sessions screen", () => {
  beforeEach(() => {
    mockPush.mockClear();
    mockListWorkspaceSessions.mockReset();
    mockListWorkspaceSessions.mockResolvedValue({ sessions: [] });
    mockSelectDm.mockReset();
    mockSelectDm.mockResolvedValue("dm-1");
  });

  it("says so when no one is sharing a terminal", async () => {
    await render(<SharedSessionsScreen />);
    await waitFor(() => expect(screen.getByText("No one is sharing a terminal right now.")).toBeTruthy());
  });

  it("shows a failed DM resolve without navigating", async () => {
    mockListWorkspaceSessions.mockResolvedValue({ sessions: [bobSession] });
    mockSelectDm.mockRejectedValue(new Error("nope"));
    await render(<SharedSessionsScreen />);
    await waitFor(() => expect(screen.getByText("Claude · Fix bug")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Open Claude · Fix bug"));
    await waitFor(() => expect(screen.getByText("this conversation could not be opened")).toBeTruthy());
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("blocks a second row's open while the first is still resolving", async () => {
    const carolSession: LocalSession = { ...bobSession, sessionId: "sess-carol", humanUserId: "carol", title: "Fix bug 2" };
    mockListWorkspaceSessions.mockResolvedValue({ sessions: [bobSession, carolSession] });
    // Never resolves within the test — the first open is still pending when the second row is tapped.
    mockSelectDm.mockReturnValue(new Promise(() => {}));
    await render(<SharedSessionsScreen />);
    await waitFor(() => expect(screen.getByText("Claude · Fix bug")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Open Claude · Fix bug"));
    await fireEvent.press(screen.getByLabelText("Open Claude · Fix bug 2"));
    expect(mockSelectDm).toHaveBeenCalledTimes(1);
  });
});
