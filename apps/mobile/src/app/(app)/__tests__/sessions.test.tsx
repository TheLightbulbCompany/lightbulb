import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { LocalSession } from "@lightbulb/shared";

const mockPush = jest.fn();
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: (...args: unknown[]) => mockPush(...args) },
    Stack: { Screen: () => null },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(cb, [cb]),
    useIsFocused: () => true,
  };
});
jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ userId: "user-a" }) }));
const mockFetchMembers = jest.fn();
// A stable reference, not a fresh {} per call -- the real useTeamsConfig
// memoizes its adapter (empty deps array), and the screen's roster effect
// depends on [config] on that promise: an unstable mock here would refire
// the effect on every setRoster-triggered render, forever.
const mockTeamsConfig = {};
jest.mock("../../../lib/teamsData", () => ({
  useTeamsConfig: () => mockTeamsConfig,
  fetchMembers: (...args: unknown[]) => mockFetchMembers(...args),
}));

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
      const selectDm = async (participant: { kind: string; id: string }, options: { activate?: boolean }) => {
        try {
          const roomId = await mockSelectDm(participant, options);
          setSelectRoomError(null);
          return roomId;
        } catch {
          setSelectRoomError("this conversation could not be opened");
          throw new Error("this conversation could not be opened");
        }
      };
      return {
        rooms: mockRoomsList,
        activeRoomId: null,
        resourceSource: mockResourceSource,
        selectDm,
        // useRooms' session-host rule (pinned in @lightbulb/shared), over this
        // double's selection: no active room, so your own goes to the first.
        sessionContextRoom: (session: { humanUserId: string }, userId?: string | null) => session.humanUserId === userId
          ? mockRoomsList[0].roomId
          : selectDm({ kind: "human", id: session.humanUserId }, { activate: false }),
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
    mockFetchMembers.mockReset();
    mockFetchMembers.mockResolvedValue([]);
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

  it("names a session's owner from the workspace roster even when the viewer shares no room with them", async () => {
    // mockRoomsList only has "bob" as a member -- carol is in no shared room,
    // so before the fix this session's section header read "Room member".
    mockFetchMembers.mockResolvedValue([{ userId: "carol", name: "Carol Danvers" }]);
    const carolSession: LocalSession = { ...bobSession, sessionId: "sess-carol", humanUserId: "carol", title: "Fix bug 2" };
    mockListWorkspaceSessions.mockResolvedValue({ sessions: [carolSession] });
    await render(<SharedSessionsScreen />);
    await waitFor(() => expect(screen.getByText("Carol Danvers")).toBeTruthy());
    expect(screen.queryByText("Room member")).toBeNull();
  });

  it("falls back to the room member's name when the roster fetch comes back empty", async () => {
    // The default mockFetchMembers from beforeEach resolves []: the shape a
    // 409 from a workspace without Teams produces (fetchMembers is
    // best-effort). bobSession's owner IS in a shared room, so the fallback
    // must still name them.
    mockListWorkspaceSessions.mockResolvedValue({ sessions: [bobSession] });
    await render(<SharedSessionsScreen />);
    await waitFor(() => expect(screen.getByText("Bob")).toBeTruthy());
  });

  it("spins pull-to-refresh only for a pull, not for a background refresh", async () => {
    const pending: ((page: { sessions: LocalSession[] }) => void)[] = [];
    mockListWorkspaceSessions.mockImplementation(() => new Promise((resolve) => { pending.push(resolve); }));
    await render(<SharedSessionsScreen />);
    // The mount fetch stands in for every 30 s poll and hint: the hook is loading.
    await waitFor(() => expect(screen.getByText("Loading shared sessions…")).toBeTruthy());
    expect(screen.getByTestId("shared-sessions").props.refreshing).toBe(false);
    await act(async () => pending.shift()!({ sessions: [] }));
    await act(async () => { screen.getByTestId("shared-sessions").props.onRefresh(); });
    expect(screen.getByTestId("shared-sessions").props.refreshing).toBe(true);
    await act(async () => pending.shift()!({ sessions: [] }));
    expect(screen.getByTestId("shared-sessions").props.refreshing).toBe(false);
  });
});
