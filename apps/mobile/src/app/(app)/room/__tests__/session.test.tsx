import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApiError, type LiveEvent, type LocalSession, type LocalSessionCommand, type LocalSessionCommandReceipt, type LocalSessionEvent, type RoomDetail, type RoomResources } from "@lightbulb/shared";
import type { ReactNode } from "react";

jest.setTimeout(20000);

jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: jest.fn(), back: jest.fn(), canGoBack: () => true, replace: jest.fn() },
    // Real react-navigation renders `options.headerRight` inside the native
    // header; this mock is the whole navigator here, so it has to do that
    // itself or the DM button would never mount at all -- see conversation.test.tsx.
    Stack: {
      Screen: ({ options }: { options?: { headerRight?: () => ReactNode } }) =>
        options?.headerRight ? options.headerRight() : null,
    },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(() => cb(), [cb]),
    useIsFocused: () => true,
    useLocalSearchParams: () => ({ roomId: "room-1", sessionId: "sess-1" }),
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
    { kind: "agent", id: "friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "active", joinedAt: "x", joinedSeq: 0 },
  ],
};
const base: LocalSession = {
  sessionId: "sess-1", deviceId: "mac-1", humanUserId: "user-a", provider: "claude", generation: "g1",
  status: "idle", providerVersion: "2.1.224", controlUnavailableReason: null, activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 5, title: "Fix relay", cwdName: "lightbulb",
};
let mockSession: LocalSession = base;
const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated"> & { truncated?: boolean }): LocalSessionEvent => ({ seq, truncated: false, ...e });
const events: LocalSessionEvent[] = [
  ev(1, { kind: "text", role: "user", text: "run it", actor: { userId: "user-a" }, ts: "2026-09-12T10:00:00.000Z" }),
  ev(2, { kind: "tool", role: "assistant", text: "git status", itemId: "t1", tool: { name: "Bash", status: "running", input: { command: "git status" } } }),
  ev(3, { kind: "tool", role: "tool", text: "S1\nS2", parentId: "t1", tool: { name: "result", status: "done", result: { stdout: "S1\nS2\nS3\nS4" }, lines: 4 } }),
  ev(4, { kind: "text", role: "assistant", text: "All green.", itemId: "m1" }),
  ev(5, { kind: "lifecycle", role: "system", text: "12000", lifecycle: "turn_end" }),
];
let mockEvents = events;
const receipt = (request: LocalSessionCommand, state: LocalSessionCommandReceipt["state"] = "applied"): LocalSessionCommandReceipt => ({
  ...request, text: request.text ?? null, expectedTurnId: request.expectedTurnId ?? null,
  sessionId: "sess-1", roomId: "room-1", actorUserId: "user-a", commandSeq: 1, state,
  createdAt: 1, deliverBy: 31, held: false, providerMessageId: null,
});
const mockListResources = jest.fn(async (): Promise<RoomResources> => ({ devices: [], sessions: [mockSession], pairedNodes: [] }));
const mockListEvents = jest.fn(async () => ({ events: mockEvents, nextAfterSeq: null }));
const mockSubmit = jest.fn(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
const mockGetCommand = jest.fn();
const mockLive = {
  httpReady: true, liveConnected: true, liveError: null, liveChannels: ["/rooms/room-1/k"], connectionRevision: 1,
  messaging: {
    listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
    getRoom: jest.fn(async () => mockRoom),
    listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
    markRead: jest.fn(async () => ({ roomId: "room-1", lastReadSeq: 3 })),
    getRunStatus: jest.fn(async () => ({ runId: "r", state: "terminal" })),
    listResources: mockListResources,
    listLocalSessionEvents: mockListEvents,
    submitLocalSessionCommand: mockSubmit,
    getLocalSessionCommand: mockGetCommand,
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

import { router } from "expo-router";

import SessionScreen from "../[roomId]/session/[sessionId]";

describe("session screen", () => {
  beforeEach(() => {
    mockScope += 1;
    mockSession = base;
    mockEvents = events;
    mockListResources.mockClear();
    mockListEvents.mockClear();
    mockSubmit.mockReset();
    mockSubmit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
    mockGetCommand.mockReset();
  });

  it("renders the transcript in the Claude grammar with the owner's attribution", async () => {
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("run it")).toBeTruthy());
    expect(mockListEvents).toHaveBeenCalledWith("room-1", "sess-1", 0, 50);
    // The summary line and the prompt's attribution both start with the owner's name.
    expect(screen.getAllByText(/^Alice · /)).toHaveLength(2);
    expect(screen.getByText("Bash(git status)")).toBeTruthy();
    expect(screen.queryByText(/S3/)).toBeNull();
    await fireEvent.press(screen.getByText("… +2 lines"));
    expect(screen.getByText(/S3/)).toBeTruthy();
    expect(screen.getByText("All green.")).toBeTruthy();
    expect(screen.getByText("Worked for 12s")).toBeTruthy();
    expect(screen.getByText("Alice · Idle · ~/lightbulb")).toBeTruthy();
    expect(screen.queryByLabelText("Working")).toBeNull();
  });

  it("shows the working line while a turn is open", async () => {
    mockSession = { ...base, status: "busy", activeTurnId: "t2", latestEventSeq: 6 };
    mockEvents = [...events, ev(6, { kind: "lifecycle", role: "system", text: "", lifecycle: "turn_start", ts: new Date().toISOString() })];
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByLabelText("Working")).toBeTruthy());
  });

  it("sends a follow-up and shows the receipt", async () => {
    await render(<SessionScreen />);
    const input = await screen.findByLabelText("Send a follow-up to Claude");
    await fireEvent.changeText(input, "  also run lint  ");
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledWith("room-1", "sess-1", {
      requestId: "33333333-3333-4333-8333-333333333333", expectedGeneration: "g1", kind: "send", text: "also run lint",
    }));
    await waitFor(() => expect(screen.getByText("Admitted to the native session — not completed work.")).toBeTruthy());
    expect(input.props.value).toBe("");
  });

  it("explains a 409 and offers check delivery after an uncertain send", async () => {
    mockSubmit.mockRejectedValueOnce(new ApiError(409, null));
    await render(<SessionScreen />);
    const input = await screen.findByLabelText("Send a follow-up to Claude");
    await fireEvent.changeText(input, "hello");
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(screen.getByText("Session changed or is offline. Refresh before sending again.")).toBeTruthy());
    mockSubmit.mockRejectedValueOnce(new TypeError("network"));
    mockGetCommand.mockResolvedValue(receipt({ requestId: "33333333-3333-4333-8333-333333333333", expectedGeneration: "g1", kind: "send", text: "hello" }, "dispatched"));
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(screen.getByLabelText("Check delivery")).toBeTruthy());
    expect(screen.getByLabelText("Retry same request")).toBeTruthy();
    await act(async () => { await fireEvent.press(screen.getByLabelText("Check delivery")); });
    await waitFor(() => expect(screen.getByText("Dispatched; native admission is not yet confirmed.")).toBeTruthy());
  });

  it("replaces the input with the reason when the session cannot take input", async () => {
    mockSession = { ...base, controlUnavailableReason: "history-gap" };
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getAllByText("History has a gap. Control is unavailable until the desktop reconnects safely.").length).toBeGreaterThan(0));
    expect(screen.queryByLabelText("Send a follow-up to Claude")).toBeNull();
  });

  it("says when the session is no longer shared", async () => {
    mockListResources.mockResolvedValue({ devices: [], sessions: [], pairedNodes: [] });
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("This session is no longer shared.")).toBeTruthy());
  });

  it("opens an ended session's transcript instead of reporting it unavailable", async () => {
    mockSession = { ...base, status: "ended" };
    // A prior test overrides listResources with a persistent mockResolvedValue
    // (mockClear in beforeEach does not undo it) — restore the default impl.
    mockListResources.mockImplementation(async () => ({ devices: [], sessions: [mockSession], pairedNodes: [] }));
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("run it")).toBeTruthy());
    expect(mockListResources).toHaveBeenCalledWith("room-1", { includeEnded: true });
    expect(screen.queryByText("This session is no longer shared.")).toBeNull();
  });

  it("has no DM button on your own session", async () => {
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("run it")).toBeTruthy());
    expect(screen.queryByLabelText("Message Alice")).toBeNull();
  });

  it("opens the sharer's DM from someone else's session", async () => {
    mockSession = { ...base, humanUserId: "user-b" };
    mockLive.messaging.createRoom.mockResolvedValueOnce({ roomId: "dm-1" } as never);
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("run it")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Message Bob"));
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/(app)/room/dm-1"));
  });

  it("keeps the transcript on screen and shows an alert when the DM open fails", async () => {
    (router.push as jest.Mock).mockClear();
    mockSession = { ...base, humanUserId: "user-b" };
    mockLive.messaging.createRoom.mockRejectedValueOnce(new Error("could not open"));
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("run it")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Message Bob"));
    await waitFor(() => expect(screen.getByText("could not open")).toBeTruthy());
    // The transcript is still the same screen underneath the banner -- a
    // failed DM open must not swap in the blocked/unavailable state.
    expect(screen.getByText("run it")).toBeTruthy();
    expect(router.push).not.toHaveBeenCalled();
  });
});
