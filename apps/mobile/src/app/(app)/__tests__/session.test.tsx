import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApiError, type LiveEvent, type LocalSession, type LocalSessionCommand, type LocalSessionCommandReceipt, type LocalSessionEvent, type RoomDetail, type SessionScreen as ScreenFrame } from "@lightbulb/shared";
import type { ReactNode } from "react";
import { AppState } from "react-native";

jest.setTimeout(20000);

jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: jest.fn(), back: jest.fn(), canGoBack: () => true, replace: jest.fn() },
    // Real react-navigation renders `options.headerRight` inside the native
    // header; this mock is the whole navigator here, so it has to do that
    // itself or the DM button would never mount at all -- see conversation.test.tsx.
    Stack: {
      Screen: ({ options }: { options?: { headerTitle?: () => ReactNode; headerRight?: () => ReactNode } }) => {
        const { createElement, Fragment } = require("react");
        return createElement(Fragment, null, options?.headerTitle?.() ?? null, options?.headerRight?.() ?? null);
      },
    },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(() => cb(), [cb]),
    useIsFocused: () => true,
    useLocalSearchParams: () => ({ sessionId: mockSessionId }),
  };
});
let mockScope = 0;
let mockSessionId = "sess-1";
const mockEventHandlers = new Set<(event: LiveEvent) => void>();
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: { id: `org-${mockScope}` } }),
}));
// The terminal's WebView: what its page posts back is driven from the tests.
let mockTerminalMessage: ((event: { nativeEvent: { data: string } }) => void) | undefined;
jest.mock("react-native-webview", () => {
  const react = require("react");
  const { View } = require("react-native");
  return {
    WebView: react.forwardRef((props: { onMessage: typeof mockTerminalMessage; testID: string }, ref: unknown) => {
      mockTerminalMessage = props.onMessage;
      react.useImperativeHandle(ref, () => ({ injectJavaScript: () => {} }));
      return react.createElement(View, { testID: props.testID });
    }),
  };
});
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }) }));
const mockFetchMembers = jest.fn();
// A stable reference -- see sessions.test.tsx's identical note: the real
// useTeamsConfig memoizes its adapter, and an unstable mock here would refire
// the roster effect on every render.
const mockTeamsConfig = {};
jest.mock("../../../lib/teamsData", () => ({
  useTeamsConfig: () => mockTeamsConfig,
  fetchMembers: (...args: unknown[]) => mockFetchMembers(...args),
}));

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
const mockListSessions = jest.fn(async (): Promise<{ sessions: LocalSession[] }> => ({ sessions: [mockSession] }));
const mockListEvents = jest.fn(async () => ({ events: mockEvents, nextAfterSeq: null }));
const mockSubmit = jest.fn(async (_s: string, request: LocalSessionCommand, _replacesRequestId?: string) => receipt(request));
const mockGetCommand = jest.fn();
const mockListOutbox = jest.fn(async () => ({ commands: [] as LocalSessionCommandReceipt[] }));
const mockGetPrivateApproval = jest.fn();
const mockAskToJoin = jest.fn(async (): Promise<LocalSession> => ({ ...mockSession, joinRequest: "pending" }));
const mockDecideJoin = jest.fn(async (): Promise<LocalSession> => mockSession);
const mockDecidePickup = jest.fn(async (): Promise<LocalSession> => mockSession);
const mockWatch = jest.fn(async (_sessionId: string, _fresh?: boolean) => {});
const mockInput = jest.fn(async (_sessionId: string, _data: string, _epoch: number) => {});
const settle = async () => { for (let i = 0; i < 50; i += 1) await Promise.resolve(); };
const fromTerminal = (message: object) => act(async () => { mockTerminalMessage?.({ nativeEvent: { data: JSON.stringify(message) } }); await settle(); });
const screenFrame = (payload: ScreenFrame) => act(async () => { mockEventHandlers.forEach((h) => h({ type: "workspace.session.screen", payload } as LiveEvent)); });
const mockLive = {
  httpReady: true, liveConnected: true, liveError: null, liveChannels: ["/rooms/room-1/k"], connectionRevision: 1,
  messaging: {
    listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
    getRoom: jest.fn(async () => mockRoom),
    listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
    markRead: jest.fn(async () => ({ roomId: "room-1", lastReadSeq: 3 })),
    getRunStatus: jest.fn(async () => ({ runId: "r", state: "terminal" })),
    listWorkspaceSessions: mockListSessions,
    // A session missing from the settled list is fetched directly; default to a genuine 404.
    getLocalSession: jest.fn(async (_sessionId: string): Promise<LocalSession> => { throw new ApiError(404, null); }),
    listLocalSessionEvents: mockListEvents,
    submitLocalSessionCommand: mockSubmit,
    getLocalSessionCommand: mockGetCommand,
    listLocalSessionOutbox: mockListOutbox,
    getPrivateSendApproval: mockGetPrivateApproval,
    requestLocalSessionJoin: mockAskToJoin,
    decideLocalSessionJoin: mockDecideJoin,
    decideLocalSessionPickup: mockDecidePickup,
    watchLocalSession: mockWatch,
    sendLocalSessionInput: mockInput,
    openSessionChat: jest.fn(async () => ({ ...mockRoom, roomId: "chat-1" })),
    sendMessage: jest.fn(), downloadAttachment: jest.fn(), attachmentUrl: jest.fn(), uploadAttachment: jest.fn(), markUnread: jest.fn(),
    setTyping: jest.fn(), abortRun: jest.fn(), steerRun: jest.fn(), createRoom: jest.fn(), updateRoom: jest.fn(),
    upsertMember: jest.fn(), removeMember: jest.fn(), updateMessage: jest.fn(), setReaction: jest.fn(),
    getLiveConfig: jest.fn(), setMemberPrefs: jest.fn(), putSections: jest.fn(),
  },
  claimActiveRoomSubscription: jest.fn(() => () => {}),
  onEvent: (handler: (event: LiveEvent) => void) => { mockEventHandlers.add(handler); return () => { mockEventHandlers.delete(handler); }; },
  onChatMessage: () => () => {},
  runtime: {}, rpc: jest.fn(), getOperation: jest.fn(),
};
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import { router } from "expo-router";

import SessionScreen from "../session/[sessionId]";

describe("session screen", () => {
  beforeEach(() => {
    mockScope += 1;
    mockSessionId = "sess-1";
    mockLive.messaging.getLocalSession.mockReset();
    mockLive.messaging.getLocalSession.mockRejectedValue(new ApiError(404, null));
    mockSession = base;
    mockEvents = events;
    mockListSessions.mockClear();
    mockListSessions.mockImplementation(async () => ({ sessions: [mockSession] }));
    mockFetchMembers.mockReset();
    mockFetchMembers.mockResolvedValue([]);
    mockListEvents.mockClear();
    mockSubmit.mockReset();
    mockSubmit.mockImplementation(async (_s: string, request: LocalSessionCommand) => receipt(request));
    mockGetCommand.mockReset();
    mockListOutbox.mockReset();
    mockListOutbox.mockResolvedValue({ commands: [] });
    mockGetPrivateApproval.mockReset();
    mockGetPrivateApproval.mockRejectedValue(new ApiError(404, null));
    mockAskToJoin.mockClear();
    mockDecideJoin.mockClear();
    mockDecidePickup.mockClear();
    mockWatch.mockClear();
    mockInput.mockClear();
  });

  it("titles the screen with the tool's mark, the session and where it runs", async () => {
    await render(<SessionScreen />);
    // Shown as the session's own title; VoiceOver still hears the tool.
    const header = await screen.findByLabelText("Claude · Fix relay, Idle on your Mac");
    expect(screen.getByText("Fix relay")).toBeTruthy();
    expect(screen.getByText("Idle on your Mac")).toBeTruthy();
    expect(header.props.accessibilityRole).toBe("button");
    expect(screen.getByTestId("session-tool-claude", { includeHiddenElements: true })).toBeTruthy();
  });

  it("never draws the history: a plain session says why its screen is not here", async () => {
    await render(<SessionScreen />);
    expect(await screen.findByText("This session was not started through Lightbulb, so its screen is not here.")).toBeTruthy();
    await waitFor(() => expect(mockListEvents).toHaveBeenCalledWith("sess-1", 0, 50));
    // The events still feed Details; none is drawn as a row.
    expect(screen.queryByText("run it")).toBeNull();
    expect(screen.queryByText("All green.")).toBeNull();
    expect(screen.queryByTestId("session-transcript")).toBeNull();
    expect(screen.queryByTestId("session-terminal")).toBeNull();
    expect(mockWatch).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByLabelText("Claude · Fix relay, Idle on your Mac"));
    expect(screen.getByText("Alice · Idle · lightbulb")).toBeTruthy();
  });

  it("shows a held session's real terminal, and the keyboard types into it: no row under it", async () => {
    mockSession = { ...base, terminal: "held" };
    await render(<SessionScreen />);
    expect(await screen.findByTestId("session-terminal")).toBeTruthy();
    expect(mockWatch.mock.calls).toEqual([["sess-1", true]]);
    expect(screen.queryByText(/not started through Lightbulb/)).toBeNull();
    // Until the picture is live the follow-up row is the way to send.
    expect(screen.getByLabelText("Send a follow-up to Claude")).toBeTruthy();
    await fromTerminal({ t: "ready" });
    await screenFrame({ sessionId: "sess-1", generation: "g1", epoch: 3, seq: 0, cols: 80, rows: 24, kind: "snapshot", data: "aGk=" });
    await waitFor(() => expect(screen.queryByLabelText("Send a follow-up to Claude")).toBeNull());
    expect(screen.getByLabelText("Terminal keys")).toBeTruthy();
    await fromTerminal({ t: "data", d: "y" });
    expect(mockInput.mock.calls).toEqual([["sess-1", "eQ==", 3]]);
  });

  it("lets a teammate watch the terminal and ask to join, with no keys", async () => {
    mockSession = { ...base, terminal: "held", humanUserId: "user-b", allowedSenders: [] };
    await render(<SessionScreen />);
    expect(await screen.findByTestId("session-terminal")).toBeTruthy();
    await fromTerminal({ t: "ready" });
    await screenFrame({ sessionId: "sess-1", generation: "g1", epoch: 3, seq: 0, cols: 80, rows: 24, kind: "snapshot", data: "aGk=" });
    expect(screen.queryByLabelText("Terminal keys")).toBeNull();
    await fromTerminal({ t: "data", d: "rm -rf /\r" });
    expect(mockInput).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByText("Ask to join"));
    await waitFor(() => expect(mockAskToJoin).toHaveBeenCalledWith("sess-1"));
  });

  it("types for a teammate the owner let in, and pauses them while the owner answers a request", async () => {
    mockSession = { ...base, terminal: "held", humanUserId: "user-b", allowedSenders: ["user-a"] };
    await render(<SessionScreen />);
    await screen.findByTestId("session-terminal");
    await fromTerminal({ t: "ready" });
    await screenFrame({ sessionId: "sess-1", generation: "g1", epoch: 3, seq: 0, cols: 80, rows: 24, kind: "snapshot", data: "aGk=" });
    expect(await screen.findByLabelText("Terminal keys")).toBeTruthy();
    mockSession = { ...mockSession, inputPaused: true };
    await act(async () => { mockEventHandlers.forEach((h) => h({ type: "workspace.sessions.changed", payload: {} } as LiveEvent)); });
    expect(await screen.findByText("Paused while Bob answers a request to join.")).toBeTruthy();
    expect(screen.queryByLabelText("Terminal keys")).toBeNull();
  });

  it("keeps an ended held session's final screen, with Resume beside it", async () => {
    mockSession = { ...base, status: "ended", endedAt: new Date(Date.now() - 5 * 60_000).toISOString(), terminal: "held", screenAvailable: true };
    await render(<SessionScreen />);
    expect(await screen.findByTestId("session-terminal")).toBeTruthy();
    expect(mockWatch.mock.calls).toEqual([["sess-1", true]]);
    expect(screen.queryByText(/screen is gone/)).toBeNull();
    expect(screen.queryByLabelText("Send a follow-up to Claude")).toBeNull();
    // Nobody types into an ended screen.
    await fromTerminal({ t: "ready" });
    await screenFrame({ sessionId: "sess-1", generation: "g1", epoch: 3, seq: 0, cols: 80, rows: 24, kind: "snapshot", data: "aGk=" });
    expect(screen.queryByLabelText("Terminal keys")).toBeNull();
  });

  it("says the screen is gone once an ended session's terminal closed", async () => {
    mockSession = { ...base, status: "ended", endedAt: new Date(Date.now() - 5 * 60_000).toISOString(), terminal: "held" };
    await render(<SessionScreen />);
    expect(await screen.findByText("The screen is gone: this session’s terminal closed.")).toBeTruthy();
    expect(screen.queryByTestId("session-terminal")).toBeNull();
  });

  it("says the owner's Mac is offline, with no terminal and no history", async () => {
    mockSession = { ...base, status: "offline", terminal: "held", humanUserId: "user-b", lastEventAt: new Date(Date.now() - 60_000).toISOString() };
    await render(<SessionScreen />);
    expect(await screen.findByText("Resume works once Bob’s Mac is back.")).toBeTruthy();
    // The header says it too.
    expect(screen.getAllByText("Bob’s Mac is offline")).toHaveLength(2);
    expect(screen.queryByTestId("session-terminal")).toBeNull();
    expect(mockWatch).not.toHaveBeenCalled();
  });

  it("sends a follow-up and shows the receipt", async () => {
    await render(<SessionScreen />);
    const input = await screen.findByLabelText("Send a follow-up to Claude");
    await fireEvent.changeText(input, "  also run lint  ");
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledWith("sess-1", {
      requestId: "33333333-3333-4333-8333-333333333333", expectedGeneration: "g1", kind: "send", text: "also run lint",
    }));
    // The follow-up stays subdued until the transcript echoes it; admission has no lasting badge.
    await waitFor(() => expect(screen.getByLabelText("Pending follow-up")).toBeTruthy());
    expect(screen.queryByText("Delivered")).toBeNull();
    expect(screen.getByText("also run lint")).toBeTruthy();
    expect(input.props.value).toBe("");
  });

  it("offers explicit Codex steer and stop when the current turn is verified", async () => {
    mockSession = { ...base, provider: "codex", status: "busy", activeTurnId: "turn-1",
      capabilities: { sendMode: "queue", steer: true, interrupt: true } };
    await render(<SessionScreen />);
    await fireEvent.changeText(screen.getByLabelText("Send a follow-up to Codex"), "change direction");
    await fireEvent.press(screen.getByLabelText("Steer current turn"));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledWith("sess-1", expect.objectContaining({
      kind: "send", text: "change direction", expectedTurnId: "turn-1",
    })));
    await fireEvent.press(screen.getByLabelText("Stop current turn"));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledWith("sess-1", expect.objectContaining({
      kind: "interrupt", expectedTurnId: "turn-1",
    })));
    expect(screen.getByText("Stop requested")).toBeTruthy();
  });

  it("lets the phone queue a follow-up while the Mac is offline", async () => {
    mockSession = { ...base, status: "offline" };
    mockSubmit.mockImplementation(async (_sessionId, request) => ({ ...receipt(request, "pending"),
      createdAt: Math.floor(Date.now() / 1000), deliverBy: Math.floor(Date.now() / 1000) + 86400 }));
    await render(<SessionScreen />);
    expect(await screen.findByText(/Mac offline. Follow-ups wait up to 24 hours/)).toBeTruthy();
    await fireEvent.changeText(screen.getByLabelText("Send a follow-up to Claude"), "resume when ready");
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(screen.getByText("Queued for session")).toBeTruthy());
  });

  it("rehydrates a queued follow-up after the phone session screen remounts", async () => {
    mockSession = { ...base, status: "offline" };
    mockListOutbox.mockResolvedValue({ commands: [{
      ...receipt({ requestId: "saved-request", expectedGeneration: "g1", kind: "send", text: "resume when ready" }, "pending"),
      createdAt: Math.floor(Date.now() / 1000), deliverBy: Math.floor(Date.now() / 1000) + 86400,
    }] });
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("resume when ready")).toBeTruthy());
    expect(screen.getByText("Queued for session")).toBeTruthy();
    expect(mockSubmit).not.toHaveBeenCalled();
  });

  it("removes the old rejected outbox action when retry starts", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockListOutbox.mockResolvedValue({ commands: [{
      ...receipt({ requestId: "saved-request", expectedGeneration: "g1", kind: "send", text: "resume when ready" }, "rejected"),
      createdAt: now - 3600, deliverBy: now - 1,
    }] });
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByLabelText("Retry follow-up")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Retry follow-up"));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    expect(mockSubmit).toHaveBeenCalledWith("sess-1", expect.objectContaining({ text: "resume when ready" }), "saved-request");
    expect(screen.queryByLabelText("Retry follow-up")).toBeNull();
  });

  it("restores the saved retry when replacement admission is rejected", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockListOutbox.mockResolvedValue({ commands: [{
      ...receipt({ requestId: "saved-request", expectedGeneration: "g1", kind: "send", text: "resume when ready" }, "rejected"),
      createdAt: now - 3600, deliverBy: now - 1,
    }] });
    mockSubmit.mockRejectedValueOnce(new ApiError(409, null));
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByLabelText("Retry follow-up")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Retry follow-up"));
    await waitFor(() => expect(screen.getByText("Session changed or is offline. Refresh before sending again.")).toBeTruthy());
    expect(screen.getByLabelText("Retry follow-up")).toBeTruthy();
  });

  it("keeps the original ID when reconciling an uncertain replacement", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockListOutbox.mockResolvedValue({ commands: [{
      ...receipt({ requestId: "saved-request", expectedGeneration: "g1", kind: "send", text: "resume when ready" }, "rejected"),
      createdAt: now - 3600, deliverBy: now - 1,
    }] });
    mockSubmit.mockRejectedValueOnce(new TypeError("network"));
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByLabelText("Retry follow-up")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Retry follow-up"));
    await waitFor(() => expect(screen.getByLabelText("Retry same request")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Retry same request"));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(2));
    expect(mockSubmit.mock.calls[1][2]).toBe("saved-request");
    expect(mockSubmit.mock.calls[1][1].requestId).toBe(mockSubmit.mock.calls[0][1].requestId);
  });

  it("clears an uncertain draft when the outbox proves admission", async () => {
    mockSession = { ...base, status: "offline" };
    const now = Math.floor(Date.now() / 1000);
    mockListOutbox.mockResolvedValue({ commands: [{
      ...receipt({ requestId: "33333333-3333-4333-8333-333333333333", expectedGeneration: "g1", kind: "send", text: "resume when ready" }, "pending"),
      createdAt: now, deliverBy: now + 86400,
    }] });
    mockSubmit.mockRejectedValueOnce(new TypeError("network"));
    await render(<SessionScreen />);
    const input = await screen.findByLabelText("Send a follow-up to Claude");
    await fireEvent.changeText(input, "resume when ready");
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(input.props.value).toBe(""));
    expect(mockSubmit).toHaveBeenCalledTimes(1);
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
    await waitFor(() => expect(screen.getByLabelText("Sending. Waiting for the session to accept it.")).toBeTruthy());
  });

  it("replaces the input with the reason when the session cannot take input", async () => {
    mockSession = { ...base, controlUnavailableReason: "history-gap" };
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getAllByText("History has a gap. Control is unavailable until the desktop reconnects safely.").length).toBeGreaterThan(0));
    expect(screen.queryByLabelText("Send a follow-up to Claude")).toBeNull();
  });


  describe("watching the terminal", () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(async () => { await cleanup(); jest.useRealTimers(); });
    const advance = async (ms: number) => { await act(async () => { await jest.advanceTimersByTimeAsync(ms); }); };

    it("stops in the background and asks for a fresh screen on return", async () => {
      // React Native's jest setup makes this a jest.fn already.
      const listen = AppState.addEventListener as jest.MockedFunction<typeof AppState.addEventListener>;
      const before = listen.mock.calls.length;
      mockSession = { ...base, terminal: "held" };
      await render(<SessionScreen />);
      await advance(0);
      expect(screen.getByTestId("session-terminal")).toBeTruthy();
      // Every listener: the screen's own, and the shared hooks'.
      const listeners = listen.mock.calls.slice(before).filter(([type]) => type === "change").map(([, handler]) => handler);
      const change = (state: "active" | "background") => act(async () => { listeners.forEach((handler) => handler(state)); });
      await advance(15_000);
      expect(mockWatch.mock.calls).toEqual([["sess-1", true], ["sess-1", false]]);
      await change("background");
      await advance(60_000);
      expect(mockWatch).toHaveBeenCalledTimes(2);
      await change("active");
      expect(mockWatch.mock.calls.at(-1)).toEqual(["sess-1", true]);
      await advance(15_000);
      expect(mockWatch.mock.calls.slice(2)).toEqual([["sess-1", true], ["sess-1", false]]);
    });
  });

  describe("direct session fallback", () => {
    beforeEach(() => { mockListSessions.mockResolvedValue({ sessions: [] }); jest.useFakeTimers(); });
    afterEach(async () => { await cleanup(); jest.useRealTimers(); });
    const get = mockLive.messaging.getLocalSession;
    const advance = async (ms: number) => { await act(async () => { await jest.advanceTimersByTimeAsync(ms); }); };

    it("recovers an ended session after a transient direct-fetch failure", async () => {
      get.mockRejectedValueOnce(new TypeError("network")).mockResolvedValue({ ...base, status: "ended" });
      await render(<SessionScreen />);
      await advance(1000);
      expect(get).toHaveBeenCalledTimes(2);
      expect(screen.getByText("Fix relay")).toBeTruthy();
    });

    it("stops after three failures and lets the person retry", async () => {
      get.mockRejectedValue(new TypeError("network"));
      await render(<SessionScreen />);
      await advance(10000);
      expect(get).toHaveBeenCalledTimes(3);
      expect(screen.getByText("Could not load this session.")).toBeTruthy();
      get.mockResolvedValue({ ...base, status: "ended" });
      await fireEvent.press(screen.getByRole("button", { name: "Try again" }));
      expect(screen.getByText("Fix relay")).toBeTruthy();
    });

    it("does not retry a genuine 404", async () => {
      await render(<SessionScreen />);
      await advance(10000);
      expect(get).toHaveBeenCalledTimes(1);
      expect(screen.getByText("This session is no longer shared.")).toBeTruthy();
    });

    const hint = async (transcript = false) => {
      await act(async () => {
        for (const handler of mockEventHandlers) handler({ type: "workspace.sessions.changed", payload: { humanUserId: "user-a", ...(transcript ? { transcript: true } : {}) } } as LiveEvent);
      });
    };

    it("keeps a slow fetch alive through several hints and coalesces revalidation", async () => {
      let resolve!: (s: LocalSession) => void;
      get.mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockImplementation(() => new Promise(() => {}));
      await render(<SessionScreen />);
      for (let i = 0; i < 4; i += 1) await hint(i % 2 === 0);
      expect(get).toHaveBeenCalledTimes(1);
      await act(async () => { resolve({ ...base, status: "ended" }); });
      expect(screen.getByText("Fix relay")).toBeTruthy();
      expect(get).toHaveBeenCalledTimes(2);
    });

    it("retries a previous 404 on a later hint", async () => {
      get.mockRejectedValueOnce(new ApiError(404, null)).mockResolvedValue({ ...base, status: "ended" });
      await render(<SessionScreen />);
      expect(screen.getByText("This session is no longer shared.")).toBeTruthy();
      await hint();
      expect(screen.getByText("Fix relay")).toBeTruthy();
    });

    it("does not reset the three-attempt budget when hints arrive during retry backoff", async () => {
      get.mockRejectedValue(new TypeError("network"));
      await render(<SessionScreen />);
      for (let i = 0; i < 4; i += 1) await hint();
      expect(get).toHaveBeenCalledTimes(1);
      await advance(3000);
      expect(screen.getByText("Could not load this session.")).toBeTruthy();
      expect(get).toHaveBeenCalledTimes(4);
    });

    it.each(["404", "exhaustion"] as const)("does not refetch a settled %s on an ordinary workspace poll", async (failure) => {
      get.mockRejectedValue(failure === "404" ? new ApiError(404, null) : new TypeError("network"));
      await render(<SessionScreen />);
      await advance(10000);
      const calls = failure === "404" ? 1 : 3;
      expect(get).toHaveBeenCalledTimes(calls);
      // Hold the 30s list poll open so loading visibly toggles on, then settles with no hint.
      let settle!: (result: { sessions: LocalSession[] }) => void;
      mockListSessions.mockImplementationOnce(() => new Promise(resolve => { settle = resolve; }));
      await advance(20000);
      await act(async () => { settle({ sessions: [] }); });
      expect(get).toHaveBeenCalledTimes(calls);
      get.mockResolvedValue({ ...base, status: "ended" });
      if (failure === "404") await hint();
      else await fireEvent.press(screen.getByRole("button", { name: "Try again" }));
      expect(screen.getByText("Fix relay")).toBeTruthy();
      expect(get).toHaveBeenCalledTimes(calls + 1);
    });

    it("keeps a loaded transcript through transient revalidation failures and permits retry", async () => {
      get.mockResolvedValueOnce({ ...base, status: "ended" }).mockRejectedValue(new TypeError("network"));
      await render(<SessionScreen />);
      await hint();
      await advance(3000);
      expect(screen.getByText("Fix relay")).toBeTruthy();
      expect(screen.getByText("Could not refresh this session.")).toBeTruthy();
      get.mockResolvedValue({ ...base, status: "ended", title: "Refreshed session" });
      await fireEvent.press(screen.getByRole("button", { name: "Try again" }));
      expect(screen.getByText("Refreshed session")).toBeTruthy();
    });

    it("discards a loaded fallback when revalidation confirms a 404", async () => {
      get.mockResolvedValueOnce({ ...base, status: "ended" }).mockRejectedValue(new ApiError(404, null));
      await render(<SessionScreen />);
      await hint();
      expect(screen.getByText("This session is no longer shared.")).toBeTruthy();
    });

    it("does not fetch metadata for transcript-only hints, but revalidates a real resources change", async () => {
      get.mockResolvedValue({ ...base, status: "ended" });
      await render(<SessionScreen />);
      for (let i = 0; i < 4; i += 1) await hint(true);
      expect(get).toHaveBeenCalledTimes(1);
      await hint(false);
      expect(get).toHaveBeenCalledTimes(2);
    });

    it("hides an old fallback failure when the workspace list supplies fresh session data", async () => {
      get.mockRejectedValue(new TypeError("network"));
      await render(<SessionScreen />);
      await advance(3000);
      expect(screen.getByText("Could not load this session.")).toBeTruthy();
      mockListSessions.mockResolvedValue({ sessions: [base] });
      await hint();
      expect(screen.getByText("Fix relay")).toBeTruthy();
      expect(screen.queryByText("Could not refresh this session.")).toBeNull();
    });

    it("retries the direct fetch even while the workspace list remains unavailable", async () => {
      mockListSessions.mockRejectedValue(new TypeError("list unavailable"));
      get.mockRejectedValue(new TypeError("network"));
      await render(<SessionScreen />);
      await advance(3000);
      expect(get).toHaveBeenCalledTimes(3);
      expect(screen.getByText("Could not refresh shared sessions.")).toBeTruthy();
      get.mockResolvedValue({ ...base, status: "ended" });
      await fireEvent.press(screen.getByRole("button", { name: "Try again" }));
      expect(get).toHaveBeenCalledTimes(4);
      expect(screen.getByText("Fix relay")).toBeTruthy();
    });

    it("fetches again on A → B → A and ignores old requests", async () => {
      let old!: (s: LocalSession) => void;
      get.mockImplementationOnce(() => new Promise(resolve => { old = resolve; })).mockImplementation(async id => ({ ...base, sessionId: id, status: "ended" }));
      const view = await render(<SessionScreen />);
      mockSessionId = "sess-2";
      await view.rerender(<SessionScreen />);
      mockSessionId = "sess-1";
      await view.rerender(<SessionScreen />);
      expect(get.mock.calls.map(([id]) => id)).toEqual(["sess-1", "sess-2", "sess-1"]);
      await act(async () => { old({ ...base, title: "Stale response" }); });
      expect(screen.getByText("Fix relay")).toBeTruthy();
    });

    it("ignores a slow response after unmount", async () => {
      let resolve!: (s: LocalSession) => void;
      get.mockImplementation(() => new Promise(r => { resolve = r; }));
      const view = await render(<SessionScreen />);
      await view.unmount();
      await act(async () => { resolve(base); });
      expect(screen.queryByText("Fix relay")).toBeNull();
      expect(get).toHaveBeenCalledTimes(1);
    });

    it("cancels a scheduled retry when unmounted", async () => {
      get.mockRejectedValue(new TypeError("network"));
      const view = await render(<SessionScreen />);
      await view.unmount();
      await advance(10000);
      expect(get).toHaveBeenCalledTimes(1);
    });
  });

  it("says when the session is no longer shared", async () => {
    mockListSessions.mockResolvedValue({ sessions: [] });
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("This session is no longer shared.")).toBeTruthy());
  });

  it("says Ended with how long ago", async () => {
    mockSession = { ...base, status: "ended", endedAt: new Date(Date.now() - 5 * 60_000).toISOString() };
    mockLive.messaging.getLocalSession.mockResolvedValue(mockSession);
    await render(<SessionScreen />);
    expect(await screen.findByText("Ended · 5m")).toBeTruthy();
  });

  it("tells a teammate how a plain session gets its screen", async () => {
    mockSession = { ...base, humanUserId: "user-b" };
    await render(<SessionScreen />);
    expect(await screen.findByText("Ask Bob to press ← in their terminal.")).toBeTruthy();
  });

  it("tells a teammate how a no-daemon Codex session gets its screen, with no ← line", async () => {
    mockSession = { ...base, humanUserId: "user-b", provider: "codex", terminalReason: "no-daemon" };
    await render(<SessionScreen />);
    expect(await screen.findByText("Ask Bob to start it again with lightbulb codex.")).toBeTruthy();
    expect(screen.queryByText(/press ←/)).toBeNull();
  });

  it("shows someone else's session read-only: no prompt row, only who can send", async () => {
    mockSession = { ...base, humanUserId: "user-b" };
    await render(<SessionScreen />);
    await waitFor(() => expect(mockListEvents).toHaveBeenCalled());
    expect(screen.getByText("Only Bob can send to this session.")).toBeTruthy();
    expect(screen.queryByLabelText("Send a follow-up to Claude")).toBeNull();
    // A backend that predates the join routes sends no `allowedSenders`, and
    // asking it would only fail.
    expect(screen.queryByText("Ask to join")).toBeNull();
  });

  it("asks to join someone else's session and reads Requested until they answer", async () => {
    mockSession = { ...base, humanUserId: "user-b", allowedSenders: [] };
    await render(<SessionScreen />);
    await fireEvent.press(await screen.findByText("Ask to join"));
    expect(mockAskToJoin).toHaveBeenCalledWith("sess-1");
    expect(await screen.findByText("Requested")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Requested" })).toBeDisabled();
  });

  it("says when the owner declined, and lets the reader ask again", async () => {
    mockSession = { ...base, humanUserId: "user-b", allowedSenders: [], joinRequest: "declined" };
    await render(<SessionScreen />);
    expect(await screen.findByText("Bob declined your request.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Ask to join" })).toBeEnabled();
  });

  it.each([
    ["the owner let them in", { allowedSenders: ["user-a"] }],
    ["the owner has team control on", { teamControl: true }],
  ])("gives a teammate the prompt row when %s", async (_why, access) => {
    mockSession = { ...base, humanUserId: "user-b", ...access };
    await render(<SessionScreen />);
    expect(await screen.findByLabelText("Send a follow-up to Claude")).toBeTruthy();
    expect(screen.queryByText("Ask to join")).toBeNull();
  });

  it("lets an invited teammate steer without offering the owner's Stop control", async () => {
    mockSession = { ...base, provider: "codex", status: "busy", activeTurnId: "turn-1",
      humanUserId: "user-b", allowedSenders: ["user-a"], teamControl: false,
      capabilities: { sendMode: "queue", steer: true, interrupt: true } };
    await render(<SessionScreen />);
    expect(await screen.findByLabelText("Steer current turn")).toBeTruthy();
    expect(screen.queryByLabelText("Stop current turn")).toBeNull();
  });

  // The request's notification opens the session, so the answer is given here.
  it("lets the owner allow a teammate who asked to join", async () => {
    mockSession = { ...base, pendingJoins: ["user-b"], pendingJoinIds: { "user-b": "req-1" } };
    await render(<SessionScreen />);
    expect(await screen.findByText("Bob wants to join this session.")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Allow Bob"));
    expect(mockDecideJoin).toHaveBeenCalledWith("sess-1", "user-b", "allow", "req-1");
    await waitFor(() => expect(screen.queryByText("Bob wants to join this session.")).toBeNull());
  });

  it("lets the owner allow a teammate who asked to pick up", async () => {
    mockSession = { ...base, pendingPickups: ["user-b"], pendingPickupIds: { "user-b": "pick-1" } };
    await render(<SessionScreen />);
    expect(await screen.findByText("Bob wants to pick up this session. Allow lets everyone in the workspace pick it up, and uploads the whole conversation and your uncommitted changes as they are. Nothing is redacted.")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Allow Bob"));
    expect(mockDecidePickup).toHaveBeenCalledWith("sess-1", "user-b", "allow", "pick-1");
    expect(mockDecideJoin).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText("Bob wants to pick up this session. Allow lets everyone in the workspace pick it up, and uploads the whole conversation and your uncommitted changes as they are. Nothing is redacted.")).toBeNull());
  });

  it("keeps the request and says so when the answer did not go through", async () => {
    mockSession = { ...base, pendingJoins: ["user-b"], pendingJoinIds: { "user-b": "req-1" } };
    mockDecideJoin.mockRejectedValueOnce(new Error("offline"));
    await render(<SessionScreen />);
    await fireEvent.press(await screen.findByLabelText("Decline Bob"));
    expect(await screen.findByText("Could not answer that request. Try again.")).toBeTruthy();
    expect(screen.getByText("Bob wants to join this session.")).toBeTruthy();
  });

  it("says 'your Mac' and 'your terminal', never \"You's\", for your own waiting session", async () => {
    mockSession = { ...base, status: "waiting-approval" };
    await render(<SessionScreen />);
    await waitFor(() => expect(mockListEvents).toHaveBeenCalled());
    // The one-line banner stays in the body.
    expect(screen.getByText("Waiting on you in your terminal")).toBeTruthy();
    // The full follow-ups/approvals sentence lives in the details sheet.
    await fireEvent.press(screen.getByLabelText("Claude · Fix relay, Waiting for input on your Mac"));
    expect(screen.getByText("2.1.224 · Follow-ups queue on your Mac. Exact Claude permissions can be answered here; questions stay in the terminal.")).toBeTruthy();
    expect(screen.getByText("Claude questions and prompts without an exact remote approval must be answered on your Mac.")).toBeTruthy();
  });

  it("shows an exact Claude prompt and sends only the answer the owner chose", async () => {
    mockSession = { ...base, status: "waiting-approval", pendingApproval: {
      approvalId: "appr-0123456789abcdef0123456789abcdef", tool: "Bash",
      summary: "git status && echo done", inputDigest: "a".repeat(64), exact: true,
      modes: ["acceptEdits"],
    } };
    await render(<SessionScreen />);
    expect(await screen.findByText("git status && echo done")).toBeTruthy();
    expect(mockSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Approve and switch to auto-accept edits" })).toBeTruthy();
    await fireEvent.press(screen.getByRole("button", { name: "Approve and switch to auto-accept edits" }));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledWith("sess-1", {
      requestId: "33333333-3333-4333-8333-333333333333", expectedGeneration: "g1",
      kind: "approve", approvalId: "appr-0123456789abcdef0123456789abcdef",
      inputDigest: "a".repeat(64), decision: "allow", mode: "acceptEdits",
    }));
    expect(await screen.findByText("Decision queued for your Mac. Waiting for the session to apply it.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("shows the owner every byte of a private send before enabling approval", async () => {
    const approvalId = "appr-0123456789abcdef0123456789abcdef";
    const inputDigest = "a".repeat(64);
    mockSession = { ...base, status: "waiting-approval", pendingApproval: {
      approvalId, inputDigest, tool: "mcp__lightbulb_native_sessions__send",
      summary: "Lightbulb message", exact: false, modes: [],
    } };
    mockGetPrivateApproval.mockResolvedValueOnce({
      approvalId, inputDigest, destination: "person:user-b", text: "First line\nSecond line",
      requestId: "e12fd3e8-32ca-4b65-8eaf-ab979e3c9449",
    });
    await render(<SessionScreen />);
    expect(await screen.findByText(/To: person:user-b/)).toBeTruthy();
    expect(screen.getByText(/First line/).props.children).toBe("First line\nSecond line");
    await fireEvent.press(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledWith("sess-1", expect.objectContaining({
      kind: "approve", approvalId, inputDigest, decision: "allow",
    })));
  });

  it("keeps a private send generic for a teammate with session control", async () => {
    mockSession = { ...base, humanUserId: "user-b", teamControl: true, status: "waiting-approval", pendingApproval: {
      approvalId: "appr-0123456789abcdef0123456789abcdef", inputDigest: "a".repeat(64),
      tool: "mcp__lightbulb_native_sessions__send", summary: "Lightbulb message", exact: false, modes: [],
    } };
    await render(<SessionScreen />);
    expect(await screen.findByText("Lightbulb message")).toBeTruthy();
    expect(mockGetPrivateApproval).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deny…" })).toBeNull();
  });

  it("retries a failed private send detail load on the phone", async () => {
    const approvalId = "appr-0123456789abcdef0123456789abcdef";
    const inputDigest = "a".repeat(64);
    mockSession = { ...base, status: "waiting-approval", pendingApproval: {
      approvalId, inputDigest, tool: "mcp__lightbulb_native_sessions__send",
      summary: "Lightbulb message", exact: false, modes: [],
    } };
    mockGetPrivateApproval.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({
      approvalId, inputDigest, destination: "room:one", text: "send this",
      requestId: "e12fd3e8-32ca-4b65-8eaf-ab979e3c9449",
    });
    await render(<SessionScreen />);
    await fireEvent.press(await screen.findByRole("button", { name: "Retry loading message" }));
    expect(await screen.findByText("send this")).toBeTruthy();
    expect(mockGetPrivateApproval).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
  });

  it("offers deny only when the exact prompt cannot be shown and keeps a failed answer visible", async () => {
    mockSession = { ...base, status: "waiting-approval", pendingApproval: {
      approvalId: "appr-0123456789abcdef0123456789abcdef", tool: "Bash",
      summary: "command is redacted", inputDigest: "b".repeat(64), exact: false, modes: [],
    } };
    mockSubmit.mockRejectedValueOnce(new ApiError(409, null));
    await render(<SessionScreen />);
    expect(await screen.findByText("command is redacted")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.getByText("Approve this in the terminal on your Mac: Lightbulb cannot show all of it. You can deny it here.")).toBeTruthy();
    await fireEvent.press(screen.getByRole("button", { name: "Deny…" }));
    await fireEvent.changeText(screen.getByLabelText("Reason for denial"), "Needs review");
    await fireEvent.press(screen.getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledWith("sess-1", {
      requestId: "33333333-3333-4333-8333-333333333333", expectedGeneration: "g1",
      kind: "approve", approvalId: "appr-0123456789abcdef0123456789abcdef",
      inputDigest: "b".repeat(64), decision: "deny", text: "Needs review",
    }));
    expect(await screen.findByText("That prompt was already answered or has changed.")).toBeTruthy();
    expect(screen.getByLabelText("Reason for denial").props.value).toBe("Needs review");
    await fireEvent.press(screen.getByRole("button", { name: "Back" }));
    await fireEvent.press(screen.getByRole("button", { name: "Deny…" }));
    expect(screen.getByLabelText("Reason for denial").props.value).toBe("Needs review");
    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
  });

  it("does not let an uninvited teammate answer", async () => {
    mockSession = { ...base, status: "waiting-approval", humanUserId: "user-b", pendingApproval: {
      approvalId: "appr-0123456789abcdef0123456789abcdef", tool: "Read",
      summary: "src/main.ts", inputDigest: "c".repeat(64), exact: true, modes: [],
    } };
    await render(<SessionScreen />);
    expect(await screen.findByText("Waiting on Bob’s approval.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Deny…" })).toBeNull();
  });

  it("keeps Codex permissions and first-run folder trust in the Mac terminal", async () => {
    mockSession = { ...base, provider: "codex", status: "waiting-approval", pendingApproval: undefined };
    await render(<SessionScreen />);
    expect(await screen.findByText("Waiting on you in your terminal")).toBeTruthy();
    await waitFor(() => expect(mockListEvents).toHaveBeenCalled());
    await fireEvent.press(screen.getByLabelText("Codex · Fix relay, Waiting for input on your Mac"));
    expect(await screen.findByText("Codex permissions and first-run folder trust must be answered on your Mac.")).toBeTruthy();
    expect(mockSubmit).not.toHaveBeenCalled();
  });

  it("has no DM button on your own session", async () => {
    await render(<SessionScreen />);
    await waitFor(() => expect(mockListEvents).toHaveBeenCalled());
    expect(screen.queryByLabelText("Message Alice")).toBeNull();
  });

  it("opens the sharer's DM from someone else's session", async () => {
    mockSession = { ...base, humanUserId: "user-b" };
    mockLive.messaging.createRoom.mockResolvedValueOnce({ roomId: "dm-1" } as never);
    await render(<SessionScreen />);
    await waitFor(() => expect(mockListEvents).toHaveBeenCalled());
    await fireEvent.press(screen.getByLabelText("Message Bob"));
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/(app)/room/dm-1"));
  });

  it("keeps the session on screen and shows an alert when the DM open fails", async () => {
    (router.push as jest.Mock).mockClear();
    mockSession = { ...base, humanUserId: "user-b" };
    mockLive.messaging.createRoom.mockRejectedValueOnce(new Error("could not open"));
    await render(<SessionScreen />);
    await waitFor(() => expect(mockListEvents).toHaveBeenCalled());
    await fireEvent.press(screen.getByLabelText("Message Bob"));
    await waitFor(() => expect(screen.getByText("could not open")).toBeTruthy());
    // The session is still the same screen underneath the banner -- a
    // failed DM open must not swap in the blocked/unavailable state.
    expect(screen.getByText("Only Bob can send to this session.")).toBeTruthy();
    expect(router.push).not.toHaveBeenCalled();
  });

  it("opens the session's chat as its own screen, with its unread count", async () => {
    (router.push as jest.Mock).mockClear();
    mockSession = { ...base, chatUnread: 2 };
    await render(<SessionScreen />);
    const chat = await screen.findByLabelText("Chat, 2 unread");
    const listed = mockLive.messaging.listWorkspaceSessions.mock.calls.length;
    await fireEvent.press(chat);
    expect(mockLive.messaging.openSessionChat).toHaveBeenCalledWith("sess-1");
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/(app)/room/chat-1"));
    // The list learns the room now, so its next message counts on the Chat button.
    await waitFor(() => expect(mockLive.messaging.listWorkspaceSessions.mock.calls.length).toBeGreaterThan(listed));
  });

  it("says so when the session's chat cannot open", async () => {
    mockLive.messaging.openSessionChat.mockRejectedValueOnce(new ApiError(404, null));
    await render(<SessionScreen />);
    await fireEvent.press(await screen.findByLabelText("Chat"));
    expect(await screen.findByText("Chat is not available for this session.")).toBeTruthy();
  });
});
