import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApiError, type LiveEvent, type LocalSession, type RoomDetail, type RoomResources } from "@lightbulb/shared";

jest.setTimeout(20000);

const mockPush = jest.fn();
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: (...args: unknown[]) => mockPush(...args), back: jest.fn(), canGoBack: () => true, replace: jest.fn() },
    Stack: { Screen: () => null },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(() => cb(), [cb]),
    useIsFocused: () => true,
    useLocalSearchParams: () => ({ roomId: "room-1" }),
  };
});
let mockScope = 0;
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: { id: `org-${mockScope}` } }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }) }));

const mockRoom: RoomDetail = {
  roomId: "room-1", name: "Launch", kind: "channel", roomRevision: 1, deliveryKey: "k",
  pinnedMessageIds: [], topic: null, starred: false, muted: false, sectionId: null,
  lastMessageSeq: 3, lastMessageAt: "2026-09-11T10:02:00Z", unreadCount: 0,
  updatedAt: "2026-09-11T10:02:00Z", lastReadSeq: 3, createdAt: "2026-09-11T09:00:00Z",
  members: [
    { kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "x", joinedSeq: 0 },
    { kind: "human", id: "user-b", displayName: "Bob", mentionHandle: null, role: "member", listenMode: null, joinedAt: "x", joinedSeq: 0 },
  ],
};
const session: LocalSession = {
  sessionId: "sess-1", deviceId: "mac-1234abcd", humanUserId: "user-a", provider: "claude", generation: "g1",
  status: "busy", providerVersion: "2.1.224", controlUnavailableReason: null, activeTurnId: "t1",
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 4, title: "Fix relay", cwdName: "lightbulb",
  lastEventAt: new Date(Date.now() - 5 * 60_000).toISOString(),
};
const full: RoomResources = {
  devices: [{ deviceId: "mac-1234abcd", humanUserId: "user-a", cliDeviceId: "cli-1" }],
  sessions: [session],
  pairedNodes: [
    { deviceId: "mac-1234abcd", humanUserId: "user-a", clientId: "openclaw-macos", macDeviceId: "mac-1234abcd" },
    { deviceId: "cli-1", humanUserId: "user-a", clientId: "node-host", macDeviceId: "mac-1234abcd" },
    { deviceId: "cli-lonely99", humanUserId: "user-b", clientId: "node-host", macDeviceId: null },
  ],
};
const endedSession: LocalSession = {
  ...session, sessionId: "sess-old", title: "Old run", status: "ended",
  endedAt: "2026-09-14T03:00:00Z", lastEventAt: "2026-09-14T03:00:00Z",
};
const mockListResources = jest.fn(async (_roomId: string, _options?: { includeEnded?: boolean }): Promise<RoomResources> => full);
const mockLive = {
  httpReady: true, liveConnected: true, liveError: null, liveChannels: ["/rooms/room-1/k"], connectionRevision: 1,
  messaging: {
    listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
    getRoom: jest.fn(async () => mockRoom),
    listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
    markRead: jest.fn(async () => ({ roomId: "room-1", lastReadSeq: 3 })),
    getRunStatus: jest.fn(async () => ({ runId: "r", state: "terminal" })),
    listResources: mockListResources,
    sendMessage: jest.fn(), downloadAttachment: jest.fn(), uploadAttachment: jest.fn(), markUnread: jest.fn(),
    setTyping: jest.fn(), abortRun: jest.fn(), steerRun: jest.fn(), createRoom: jest.fn(), updateRoom: jest.fn(),
    upsertMember: jest.fn(), removeMember: jest.fn(), updateMessage: jest.fn(), setReaction: jest.fn(),
    getLiveConfig: jest.fn(), setMemberPrefs: jest.fn(), putSections: jest.fn(),
  },
  claimActiveRoomSubscription: jest.fn(() => () => {}),
  onEvent: (_handler: (event: LiveEvent) => void) => () => {},
  onChatMessage: () => () => {},
  runtime: {}, rpc: jest.fn(), getOperation: jest.fn(),
};
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import DevicesScreen from "../[roomId]/devices";

describe("devices & sessions screen", () => {
  beforeEach(() => {
    mockScope += 1;
    mockPush.mockClear();
    mockListResources.mockReset();
    mockListResources.mockResolvedValue(full);
  });

  it("groups Macs and sessions by member and opens a session", async () => {
    await render(<DevicesScreen />);
    await waitFor(() => expect(screen.getByText("Alice")).toBeTruthy());
    expect(screen.getByText("Mac · mac-1234")).toBeTruthy();
    expect(screen.getByText("Paired Mac + associated CLI · availability unknown")).toBeTruthy();
    expect(screen.getByText("Claude · Fix relay")).toBeTruthy();
    expect(screen.getByText(/Working · 5m · ~\/lightbulb/)).toBeTruthy();
    expect(screen.getByText("Bob")).toBeTruthy();
    expect(screen.getByText("Paired CLI endpoint · cli-lone")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Open Claude · Fix relay"));
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-1/session/sess-1");
  });

  it("hides ended sessions until Show recent", async () => {
    mockListResources.mockImplementation((_room: string, options?: { includeEnded?: boolean }) =>
      Promise.resolve({ ...full, sessions: options?.includeEnded ? [...full.sessions, endedSession] : full.sessions }));
    await render(<DevicesScreen />);
    await waitFor(() => expect(screen.getByText("Claude · Fix relay")).toBeTruthy());
    expect(screen.queryByText("Claude · Old run")).toBeNull();
    await fireEvent.press(screen.getByText("Show recent"));
    await waitFor(() => expect(screen.getByText("Claude · Old run")).toBeTruthy());
    expect(screen.getByText(/Ended/)).toBeTruthy();
  });

  it("says so when nothing is shared", async () => {
    mockListResources.mockResolvedValue({ devices: [], sessions: [], pairedNodes: [] });
    await render(<DevicesScreen />);
    await waitFor(() => expect(screen.getByText("No paired devices or shared sessions in this conversation.")).toBeTruthy());
  });

  it("shows the refresh error with a retry that loads the list", async () => {
    mockListResources.mockRejectedValueOnce(new TypeError("offline")).mockResolvedValueOnce(full);
    await render(<DevicesScreen />);
    await waitFor(() => expect(screen.getByText("Could not refresh devices and sessions. Try again.")).toBeTruthy());
    expect(screen.queryByText("Claude · Fix relay")).toBeNull();
    await fireEvent.press(screen.getByLabelText("Try again"));
    await waitFor(() => expect(screen.getByText("Claude · Fix relay")).toBeTruthy());
    expect(screen.queryByText("Could not refresh devices and sessions. Try again.")).toBeNull();
  });

  it("reports removed access", async () => {
    mockListResources.mockRejectedValue(new ApiError(403, null));
    await render(<DevicesScreen />);
    await waitFor(() => expect(screen.getByText(/Access to these resources was removed/)).toBeTruthy());
  });
});
