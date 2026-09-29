import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react-native";
import { ApiError, type LiveEvent, type LocalSession, type LocalSessionCommand, type LocalSessionCommandReceipt, type LocalSessionEvent, type RoomDetail } from "@lightbulb/shared";
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
      Screen: ({ options }: { options?: { headerTitle?: () => ReactNode; headerRight?: () => ReactNode } }) => {
        const { createElement, Fragment } = require("react");
        return createElement(Fragment, null, options?.headerTitle?.() ?? null, options?.headerRight?.() ?? null);
      },
    },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(() => cb(), [cb]),
    useIsFocused: () => true,
    useLocalSearchParams: () => ({ sessionId: "sess-1" }),
  };
});
let mockScope = 0;
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: { id: `org-${mockScope}` } }),
}));
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
const mockSubmit = jest.fn(async (_s: string, request: LocalSessionCommand) => receipt(request));
const mockGetCommand = jest.fn();
const mockAskToJoin = jest.fn(async (): Promise<LocalSession> => ({ ...mockSession, joinRequest: "pending" }));
const mockDecideJoin = jest.fn(async (): Promise<LocalSession> => mockSession);
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
    getLocalSession: jest.fn(async () => { throw new ApiError(404, null); }),
    listLocalSessionEvents: mockListEvents,
    submitLocalSessionCommand: mockSubmit,
    getLocalSessionCommand: mockGetCommand,
    requestLocalSessionJoin: mockAskToJoin,
    decideLocalSessionJoin: mockDecideJoin,
    sendMessage: jest.fn(), downloadAttachment: jest.fn(), attachmentUrl: jest.fn(), uploadAttachment: jest.fn(), markUnread: jest.fn(),
    setTyping: jest.fn(), abortRun: jest.fn(), steerRun: jest.fn(), createRoom: jest.fn(), updateRoom: jest.fn(),
    upsertMember: jest.fn(), removeMember: jest.fn(), updateMessage: jest.fn(), setReaction: jest.fn(),
    getLiveConfig: jest.fn(), setMemberPrefs: jest.fn(), putSections: jest.fn(),
  },
  claimActiveRoomSubscription: jest.fn(() => () => {}),
  onEvent: (_handler: (event: LiveEvent) => void) => () => {},
  onChatMessage: () => () => {},
  runtime: {}, rpc: jest.fn(), getOperation: jest.fn(),
};
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import { router } from "expo-router";

import SessionScreen from "../session/[sessionId]";

describe("session screen", () => {
  beforeEach(() => {
    mockScope += 1;
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
    mockAskToJoin.mockClear();
    mockDecideJoin.mockClear();
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

  it("renders the transcript in the Claude grammar with the owner's attribution", async () => {
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("run it")).toBeTruthy());
    expect(mockListEvents).toHaveBeenCalledWith("sess-1", 0, 50);
    // The prompt's attribution: the owner viewing their own session sees "You".
    expect(screen.getByLabelText("You: run it")).toBeTruthy();
    // Claude Code folds even a lone shell command into one line; opening it shows the call.
    expect(screen.queryByText("Bash(git status)")).toBeNull();
    await fireEvent.press(screen.getByText("Ran 1 shell command"));
    expect(screen.getByText("Bash(git status)")).toBeTruthy();
    expect(screen.queryByText(/S3/)).toBeNull();
    // The tool well is collapsed behind the chip; open it before its own "show more".
    await fireEvent.press(screen.getByText("Bash(git status)"));
    await fireEvent.press(screen.getByText("… +2 lines"));
    expect(screen.getByText(/S3/)).toBeTruthy();
    expect(screen.getByText("All green.")).toBeTruthy();
    expect(screen.getByText("Worked for 12s")).toBeTruthy();
    // The two grey summary lines moved into the details sheet the header opens.
    await fireEvent.press(screen.getByLabelText("Claude · Fix relay, Idle on your Mac"));
    expect(screen.getByText("Alice · Idle · ~/lightbulb")).toBeTruthy();
    expect(screen.queryByLabelText("Working")).toBeNull();
  });

  it("shows the working line while a turn is open", async () => {
    mockSession = { ...base, status: "busy", activeTurnId: "t2", latestEventSeq: 6 };
    mockEvents = [...events, ev(6, { kind: "lifecycle", role: "system", text: "", lifecycle: "turn_start", ts: new Date().toISOString() })];
    await render(<SessionScreen />);
    // A busy session also rings the header avatar "Working" -- scope to the
    // transcript so this asserts the working row specifically.
    const list = await screen.findByTestId("session-transcript");
    await waitFor(() => expect(within(list).getByLabelText("Working")).toBeTruthy());
  });

  it("sends a follow-up and shows the receipt", async () => {
    await render(<SessionScreen />);
    const input = await screen.findByLabelText("Send a follow-up to Claude");
    await fireEvent.changeText(input, "  also run lint  ");
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledWith("sess-1", {
      requestId: "33333333-3333-4333-8333-333333333333", expectedGeneration: "g1", kind: "send", text: "also run lint",
    }));
    // The follow-up shows at once as a pending row carrying one status chip.
    await waitFor(() => expect(screen.getByText("Delivered")).toBeTruthy());
    expect(screen.getByLabelText("Pending follow-up")).toBeTruthy();
    expect(screen.getByText("also run lint")).toBeTruthy();
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
    await waitFor(() => expect(screen.getByLabelText("Sending. Waiting for the session to accept it.")).toBeTruthy());
  });

  it("replaces the input with the reason when the session cannot take input", async () => {
    mockSession = { ...base, controlUnavailableReason: "history-gap" };
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getAllByText("History has a gap. Control is unavailable until the desktop reconnects safely.").length).toBeGreaterThan(0));
    expect(screen.queryByLabelText("Send a follow-up to Claude")).toBeNull();
  });

  it("says when the session is no longer shared", async () => {
    mockListSessions.mockResolvedValue({ sessions: [] });
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("This session is no longer shared.")).toBeTruthy());
  });

  it("shows someone else's session read-only: no prompt row, only who can send", async () => {
    mockSession = { ...base, humanUserId: "user-b" };
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("run it")).toBeTruthy());
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

  // The request's notification opens the session, so the answer is given here.
  it("lets the owner allow a teammate who asked to join", async () => {
    mockSession = { ...base, pendingJoins: ["user-b"] };
    await render(<SessionScreen />);
    expect(await screen.findByText("Bob wants to join this session.")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Allow Bob"));
    expect(mockDecideJoin).toHaveBeenCalledWith("sess-1", "user-b", "allow");
    await waitFor(() => expect(screen.queryByText("Bob wants to join this session.")).toBeNull());
  });

  it("keeps the request and says so when the answer did not go through", async () => {
    mockSession = { ...base, pendingJoins: ["user-b"] };
    mockDecideJoin.mockRejectedValueOnce(new Error("offline"));
    await render(<SessionScreen />);
    await fireEvent.press(await screen.findByLabelText("Decline Bob"));
    expect(await screen.findByText("Could not answer that request. Try again.")).toBeTruthy();
    expect(screen.getByText("Bob wants to join this session.")).toBeTruthy();
  });

  it("says 'your Mac' and 'your terminal', never \"You's\", for your own waiting session", async () => {
    mockSession = { ...base, status: "waiting-approval" };
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("run it")).toBeTruthy());
    // The one-line banner stays in the body.
    expect(screen.getByText("Waiting on you in your terminal")).toBeTruthy();
    // The full follow-ups/approvals sentence lives in the details sheet.
    await fireEvent.press(screen.getByLabelText("Claude · Fix relay, Waiting for input on your Mac"));
    expect(screen.getByText("2.1.224 · Follow-ups queue on your Mac. Approvals stay in your terminal.")).toBeTruthy();
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

  // 2000 of content in an 800 window, scrolled 1200 from the newest: at the top.
  const NEAR_TOP = {
    nativeEvent: { contentOffset: { x: 0, y: 1200 }, contentSize: { width: 390, height: 2000 }, layoutMeasurement: { width: 390, height: 800 } },
  };

  it("fills the screen on open when the newest page draws only a few rows", async () => {
    mockSession = { ...base, latestEventSeq: 120 };
    mockEvents = [116, 117, 118, 119, 120].map((seq) =>
      ev(seq, { kind: "text", role: "user", text: `line ${seq}`, actor: { userId: "user-a" }, ts: "2026-09-12T10:00:00.000Z" }),
    );
    await render(<SessionScreen />);
    const list = await screen.findByTestId("session-transcript");
    // Measured: an 800 window holding 300 of transcript is not full.
    await act(async () => {
      list.props.onLayout?.({ nativeEvent: { layout: { x: 0, y: 0, width: 390, height: 800 } } });
      list.props.onContentSizeChange?.(390, 300);
    });
    await waitFor(() => expect(mockListEvents).toHaveBeenCalledWith("sess-1", 65, 50));
  });

  it("loads older history when the reader scrolls to the top", async () => {
    mockSession = { ...base, latestEventSeq: 120 };
    // useLocalSessionHistory marks hasOlder once the first page starts above
    // seq 1 -- five events seqed 116-120 gives it that first page.
    mockEvents = [116, 117, 118, 119, 120].map((seq) =>
      ev(seq, { kind: "text", role: "user", text: `line ${seq}`, actor: { userId: "user-a" }, ts: "2026-09-12T10:00:00.000Z" }),
    );
    await render(<SessionScreen />);
    const list = await screen.findByTestId("session-transcript");
    // Inverted: the far end of the scroll is the top of the transcript.
    await act(async () => { list.props.onScroll?.(NEAR_TOP); });
    // first seq 116 -> the older page asks for after=65 (116-51), limit 50.
    await waitFor(() => expect(mockListEvents).toHaveBeenCalledWith("sess-1", 65, 50));
  });

  it("opens on the newest output and follows it: newest first, no catch-up link", async () => {
    mockSession = { ...base, status: "busy", latestEventSeq: 120 };
    mockEvents = [116, 117, 118, 119, 120].map((seq) =>
      ev(seq, { kind: "text", role: "assistant", text: `line ${seq}`, ts: "2026-09-12T10:00:00.000Z" }),
    );
    await render(<SessionScreen />);
    const list = await screen.findByTestId("session-transcript");
    await waitFor(() => expect(list.props.data.length).toBeGreaterThan(0));
    expect(list.props.inverted).toBe(true);
    const seqs = list.props.data.map((row: { seq: number }) => row.seq);
    expect(seqs).toEqual([...seqs].sort((a: number, b: number) => b - a));
    // Paging back must not turn the screen into a history view that needs a tap.
    await act(async () => { list.props.onScroll?.(NEAR_TOP); });
    await waitFor(() => expect(mockListEvents).toHaveBeenCalledWith("sess-1", 65, 50));
    expect(screen.queryByText(/More recent activity/)).toBeNull();
  });

  it("offers a way back to the bottom only once the reader has scrolled up", async () => {
    await render(<SessionScreen />);
    const list = await screen.findByTestId("session-transcript");
    expect(screen.queryByLabelText("Jump to latest")).toBeNull();
    const scrolledTo = (y: number) => ({
      nativeEvent: { contentOffset: { x: 0, y }, contentSize: { width: 390, height: 2000 }, layoutMeasurement: { width: 390, height: 800 } },
    });
    await act(async () => { list.props.onScroll?.(scrolledTo(600)); });
    expect(screen.getByLabelText("Jump to latest")).toBeTruthy();
    await act(async () => { list.props.onScroll?.(scrolledTo(0)); });
    expect(screen.queryByLabelText("Jump to latest")).toBeNull();
  });
});
