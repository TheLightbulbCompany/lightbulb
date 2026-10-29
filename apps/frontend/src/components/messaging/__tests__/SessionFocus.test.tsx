import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ApiError, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { SessionFocus, sessionChatLayout } from "../SessionFocus";

vi.mock("../SessionChat", () => ({ SessionChat: ({ onClose, openChat, onRead }: { onClose: () => void; openChat: (id: string) => Promise<unknown>; onRead?: () => void }) =>
  <aside aria-label="Session chat"><button onClick={onClose}>Close chat</button><button onClick={() => void openChat("s1")}>Open door</button><button onClick={onRead}>Read</button></aside> }));

const session: LocalSession = { sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", terminal: "held", providerVersion: "2.1.269", generation: "g", status: "busy", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, title: "Load test" };
const resolveSessionOwner = () => ({ ownerName: "Alice", own: false });
/** The pane's one-line status row: status, owner, then where it runs. */
const statusLine = () => document.querySelector<HTMLElement>(".session-status-line")!;
async function openDetails() {
  await userEvent.click(await screen.findByRole("button", { name: "Details" }));
  return screen.findByRole("dialog", { name: "Load test" });
}

function focus(entry: { session: LocalSession; ownerName: string; own: boolean } | null, { onClose = vi.fn(), getLocalSession, events = [], submit, onMessageOwner, participants, join, decide, askPickup, decidePickup }: {
  onClose?: () => void; getLocalSession?: SessionSource["client"]["getLocalSession"];
  events?: unknown[]; submit?: SessionSource["client"]["submitLocalSessionCommand"];
  onMessageOwner?: (id: string, name: string) => void; participants?: { kind: "human" | "agent"; id: string; displayName: string }[];
  join?: (sessionId: string) => Promise<LocalSession>;
  decide?: (sessionId: string, userId: string, decision: "allow" | "decline", requestId?: string) => Promise<LocalSession>;
  askPickup?: (sessionId: string) => Promise<LocalSession>;
  decidePickup?: (sessionId: string, userId: string, decision: "allow" | "decline", requestId?: string) => Promise<LocalSession>;
} = {}) {
  const client = {
    listWorkspaceSessions: vi.fn(),
    getLocalSession: getLocalSession ?? vi.fn(async () => { throw new ApiError(404, null); }),
    listLocalSessionEvents: vi.fn(async () => ({ events, nextAfterSeq: null })),
    submitLocalSessionCommand: submit ?? vi.fn(),
    getLocalSessionCommand: vi.fn(),
    requestLocalSessionPickup: vi.fn(),
    ...(join ? { requestLocalSessionJoin: join } : {}),
    ...(decide ? { decideLocalSessionJoin: decide } : {}),
    ...(askPickup ? { requestLocalSessionPickupAccess: askPickup } : {}),
    ...(decidePickup ? { decideLocalSessionPickup: decidePickup } : {}),
  } as unknown as SessionSource["client"];
  return render(<SessionFocus focus={{ sessionId: "s1" }} entries={entry ? [entry] : []} hintRevision={0} client={client}
    resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={onClose} onMessageOwner={onMessageOwner} participants={participants} />);
}

it("renders someone else's session alone, read-only, with no conversation column", async () => {
  focus({ session, ownerName: "Alice", own: false });
  const heading = await screen.findByRole("heading", { name: "Claude Load test" });
  expect(heading).toHaveTextContent(/^Load test$/);
  expect(screen.queryByRole("complementary", { name: "Conversation" })).not.toBeInTheDocument();
  expect(screen.getByText("Only Alice can type in this session.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Send follow-up" })).not.toBeInTheDocument();
});

it("renders your own session with its composer and closes", async () => {
  const onClose = vi.fn();
  focus({ session, ownerName: "You", own: true }, { onClose });
  expect(await screen.findByRole("heading", { name: /Load test/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeInTheDocument();
  screen.getByRole("button", { name: "Close session" }).click();
  expect(onClose).toHaveBeenCalled();
});

it("falls back to a direct fetch when the workspace list lacks the session, and shows a 404 as no longer shared", async () => {
  focus(null);
  expect(await screen.findByText("This session is no longer shared.")).toBeInTheDocument();
});

it("reads the session by id at once, without waiting for the workspace list (prod 2026-10-27: 2.6-3.8 s)", () => {
  const getLocalSession = vi.fn(() => new Promise<LocalSession>(() => {}));
  focus(null, { getLocalSession });
  expect(screen.getByText("Opening session…")).toBeInTheDocument();
  expect(getLocalSession).toHaveBeenCalledWith("s1");
});

it("falls back to a direct fetch and renders an ended session as the Ended panel, with Resume and no follow-up box", async () => {
  const ended: LocalSession = { ...session, status: "ended" };
  focus(null, { getLocalSession: vi.fn(async () => ended) });
  await screen.findByText("This session ended before anything was sent.");
  expect(within(statusLine()).getByText("Ended")).toBeInTheDocument();
  expect(statusLine().textContent).not.toMatch(/Transcript/);
  expect(within(statusLine()).getByText("Alice")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Send follow-up" })).not.toBeInTheDocument();
  expect(screen.queryByText("Only Alice can type in this session.")).not.toBeInTheDocument();
});

it("says 'your Mac' and 'your terminal', never \"You's\", for your own waiting session", async () => {
  focus({ session: { ...session, status: "waiting-approval" }, ownerName: "You", own: true });
  expect(await screen.findByText("Waiting on you in your terminal")).toBeInTheDocument();
  expect(within(statusLine()).getByText("Waiting for input")).toBeInTheDocument();
  expect(within(statusLine()).getByText("You")).toBeInTheDocument();
  // The trust copy lives in Details, on its About tab.
  expect(screen.queryByText(/^Follow-ups queue/)).not.toBeInTheDocument();
  const about = await openDetails();
  expect(within(about).getByText(/^Follow-ups queue behind the current turn on your Mac\. They run with your permissions and count against your usage\./)).toBeInTheDocument();
  expect(within(about).queryByText(/You's/)).not.toBeInTheDocument();
});

it("shows the session title once, in the header, with one compact status line", async () => {
  focus({ session: { ...session, cwdName: "lightbulb" }, ownerName: "Alice", own: false });
  const heading = await screen.findByRole("heading", { name: "Claude Load test" });
  expect(heading).toHaveTextContent(/^Load test$/);
  expect(screen.getAllByText("Load test")).toHaveLength(1);
  expect(document.querySelectorAll(".session-status-line")).toHaveLength(1);
  // The Mac reports the folder's own name and nothing above it, so no "~/": it is not a path from home.
  expect([...statusLine().children].map(span => span.textContent)).toEqual(["", "Working", "Alice", "lightbulb"]);
});

it("has no second link in Details: the one link is under Access", async () => {
  focus({ session, ownerName: "Alice", own: false });
  const details = await openDetails();
  expect(within(details).queryByRole("button", { name: /Copy link/ })).not.toBeInTheDocument();
});

it("keeps the follow-up composer compact: hidden label, short placeholder, icon send", async () => {
  focus({ session, ownerName: "You", own: true });
  const field = await screen.findByRole("textbox", { name: "Send to Claude · Load test" });
  expect(field).toHaveAttribute("placeholder", "Send a follow-up…");
  expect(screen.getByText("Send to Claude · Load test")).toHaveClass("sr-only");
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeDisabled();
});

it("shows a waiting banner with the reason and a Message owner action, never a remote approve", async () => {
  const onMessageOwner = vi.fn();
  focus({ session: { ...session, status: "waiting-approval" }, ownerName: "Alice", own: false }, {
    onMessageOwner, events: [{ seq: 1, kind: "lifecycle", lifecycle: "approval_pending", text: "Bash(rm -rf build)", role: "system" }],
  });
  expect(await screen.findByText("Waiting on Alice in their terminal")).toBeInTheDocument();
  expect(await screen.findByText("Bash(rm -rf build)", { selector: ".local-session-waiting-reason" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Message Alice" }));
  expect(onMessageOwner).toHaveBeenCalledWith("alice", "Alice");
});

it("offers Message owner beside the read-only line and in Details", async () => {
  const onMessageOwner = vi.fn();
  focus({ session, ownerName: "Alice", own: false }, { onMessageOwner });
  await userEvent.click(await screen.findByRole("button", { name: "Message Alice" }));
  const details = await openDetails();
  await userEvent.click(within(details).getByRole("button", { name: "Message Alice" }));
  expect(onMessageOwner).toHaveBeenCalledTimes(2);
});

it("explains that an offline follow-up waits for the Mac", async () => {
  focus({ session: { ...session, status: "offline" }, ownerName: "You", own: true });
  expect(await screen.findByText("Mac offline. Follow-ups wait up to 24 hours for this session to reconnect.")).toBeInTheDocument();
  await userEvent.type(screen.getByRole("textbox", { name: "Send to Claude · Load test" }), "continue");
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeEnabled();
});

it("shows a sent follow-up at once as a quiet greyed pending row", async () => {
  const submit = vi.fn(async (_s: string, request: { requestId: string; text?: string }) => ({
    ...request, expectedGeneration: "g", kind: "send", text: request.text ?? null, expectedTurnId: null, sessionId: "s1", roomId: "r",
    actorUserId: "alice", commandSeq: 1, state: "applied", createdAt: 1, deliverBy: 31, held: true, providerMessageId: null,
  }));
  focus({ session: { ...session, status: "idle" }, ownerName: "You", own: true }, { submit: submit as never });
  const field = await screen.findByRole("textbox", { name: "Send to Claude · Load test" });
  await userEvent.type(field, "run the tests{Enter}");
  const row = await screen.findByRole("article", { name: "Pending follow-up" });
  expect(within(row).getByText("run the tests")).toBeInTheDocument();
  expect(row).toHaveClass("native-session-pending");
  expect(within(row).queryByRole("status")).not.toBeInTheDocument();
});

it("offers a teammate no Ask to join: they watch until the owner lets teammates control it, and can still ask to pick up", async () => {
  const askPickup = vi.fn(async () => ({ ...session, pickupRequest: "pending" as const }));
  focus({ session, ownerName: "Alice", own: false }, { join: vi.fn(), askPickup });
  expect(await screen.findByText("Only Alice can type in this session.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Ask to join" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Ask to pick up" }));
  expect(askPickup).toHaveBeenCalledWith("s1");
});

it("lets the owner answer a request to join in the session itself", async () => {
  const decide = vi.fn(async () => session);
  // This file's name lookup answers "Alice" for everyone.
  focus({ session: { ...session, status: "idle", pendingJoins: ["bob"], pendingJoinIds: { bob: "req-1" } }, ownerName: "You", own: true }, { decide });
  expect(await screen.findByText("Alice wants to join this session.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Allow" }));
  expect(decide).toHaveBeenCalledWith("s1", "bob", "allow", "req-1");
});

it("gives a person the owner allowed the composer", async () => {
  focus({ session: { ...session, allowedSenders: ["user_test_123"] }, ownerName: "Alice", own: false });
  expect(await screen.findByRole("button", { name: "Send follow-up" })).toBeInTheDocument();
  expect(screen.queryByText("Only Alice can type in this session.")).not.toBeInTheDocument();
});

it("opens the session a Claude session moved to the background continues as", async () => {
  focus({ session: { ...session, sessionId: "s2", forkedFrom: "s1", title: "Moved on" }, ownerName: "Alice", own: false });
  expect(await screen.findByRole("heading", { name: "Claude Moved on" })).toBeInTheDocument();
});

it("opens a live origin, not a fork copied from it", async () => {
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() } as unknown as SessionSource["client"];
  const entries = [
    { session: { ...session, sessionId: "s2", forkedFrom: "s1", title: "The fork" }, ownerName: "Alice", own: false },
    { session, ownerName: "Alice", own: false },
  ];
  render(<SessionFocus focus={{ sessionId: "s1" }} entries={entries} hintRevision={0} client={client}
    resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={vi.fn()} />);
  expect(await screen.findByRole("heading", { name: "Claude Load test" })).toBeInTheDocument();
});

const ready = { allowed: true, state: "ready" } as never;

it("lets a teammate ask to pick up, then reads Requested", async () => {
  const askPickup = vi.fn(async () => ({ ...session, pickupRequest: "pending" as const }));
  focus({ session, ownerName: "Alice", own: false }, { askPickup });
  await userEvent.click(await screen.findByRole("button", { name: "Ask to pick up" }));
  expect(askPickup).toHaveBeenCalledWith("s1");
  expect(await screen.findByRole("button", { name: "Requested" })).toBeDisabled();
});

it("says the owner declined the pick-up request and lets the person ask again", async () => {
  focus({ session: { ...session, pickupRequest: "declined" }, ownerName: "Alice", own: false }, { askPickup: vi.fn() });
  expect(await screen.findByRole("button", { name: "Alice declined. Ask again" })).toBeEnabled();
});

it("shows Pick up, not Ask to pick up, once pick-up is allowed and ready", async () => {
  focus({ session: { ...session, pickup: ready }, ownerName: "Alice", own: false }, { askPickup: vi.fn() });
  expect(await screen.findByRole("button", { name: /Pick up/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Ask to pick up" })).not.toBeInTheDocument();
});

it("offers Ask to pick up on an ended Claude session, whose snapshot is kept", async () => {
  focus({ session: { ...session, status: "ended" }, ownerName: "Alice", own: false }, { askPickup: vi.fn() });
  expect(await screen.findByRole("button", { name: "Ask to pick up" })).toBeEnabled();
});

it("offers no Ask to pick up on a Codex session", async () => {
  focus({ session: { ...session, provider: "codex" }, ownerName: "Alice", own: false }, { askPickup: vi.fn() });
  expect(await screen.findByRole("heading", { name: /Load test/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Ask to pick up" })).not.toBeInTheDocument();
});

it("lets the owner answer a pick-up request in the session itself", async () => {
  const decidePickup = vi.fn(async () => session);
  focus({ session: { ...session, status: "idle", pendingPickups: ["bob"], pendingPickupIds: { bob: "req-9" } }, ownerName: "You", own: true }, { decidePickup });
  expect(await screen.findByText("Alice wants to pick up this session. Allow lets everyone in the workspace pick it up, and uploads the whole conversation and your uncommitted changes as they are. Nothing is redacted.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Allow" }));
  expect(decidePickup).toHaveBeenCalledWith("s1", "bob", "allow", "req-9");
});

it("reads Preparing… (disabled) while pick-up is allowed but the snapshot is not ready", async () => {
  focus({ session: { ...session, pickup: { allowed: true, state: "preparing" } }, ownerName: "Alice", own: false }, { askPickup: vi.fn() });
  expect(await screen.findByRole("button", { name: "Preparing…" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Ask to pick up" })).not.toBeInTheDocument();
});

it("says why when the snapshot is unavailable", async () => {
  focus({ session: { ...session, pickup: { allowed: true, state: "unavailable", reason: "too-large" } }, ownerName: "Alice", own: false }, { askPickup: vi.fn() });
  expect(await screen.findByText("This session is too large to snapshot.")).toBeInTheDocument();
});

function chatFocus(chatOpen: boolean, chatUnread?: number) {
  const onChatOpenChange = vi.fn();
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), openSessionChat: vi.fn() } as unknown as SessionSource["client"];
  render(<SessionFocus focus={{ sessionId: "s1" }} entries={[{ session: { ...session, chatUnread, chatRoomId: "chat-1" }, ownerName: "Alice", own: false }]}
    hintRevision={0} client={client} resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={vi.fn()}
    chatOpen={chatOpen} onChatOpenChange={onChatOpenChange} />);
  return onChatOpenChange;
}

it("offers the session's chat with its unread count and opens it beside the terminal", async () => {
  const onChatOpenChange = chatFocus(false, 2);
  await userEvent.click(await screen.findByRole("button", { name: "Chat, 2 unread" }));
  expect(onChatOpenChange).toHaveBeenCalledWith(true);
  expect(screen.queryByRole("complementary", { name: "Session chat" })).not.toBeInTheDocument();
});

it("renders the chat column while open and closes it from either side", async () => {
  const onChatOpenChange = chatFocus(true, 2);
  expect(await screen.findByRole("complementary", { name: "Session chat" })).toBeInTheDocument();
  expect(document.querySelector(".room-session-focus.session-chat-open")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Chat" })).toHaveAttribute("aria-pressed", "true"); // no count while reading
  await userEvent.click(screen.getByRole("button", { name: "Close chat" }));
  expect(onChatOpenChange).toHaveBeenCalledWith(false);
});

it("shows the server's chat unread count and never adds live arrivals on top (the list refetches on one)", async () => {
  let emit: (event: unknown) => void = () => {};
  const onEvent = ((handler: (event: unknown) => void) => { emit = handler; return () => {}; }) as unknown as SessionSource["onEvent"];
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), openSessionChat: vi.fn() } as unknown as SessionSource["client"];
  const view = (chatUnread: number) => <SessionFocus focus={{ sessionId: "s1" }} entries={[{ session: { ...session, chatRoomId: "chat-1", chatUnread }, ownerName: "Alice", own: false }]}
    hintRevision={0} client={client} onEvent={onEvent} resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={vi.fn()} chatOpen={false} onChatOpenChange={vi.fn()} />;
  const { rerender } = render(view(1));
  await screen.findByRole("button", { name: "Chat, 1 unread" });
  act(() => emit({ type: "room.message.available", payload: { roomId: "chat-1", threadRootMessageId: null } }));
  expect(screen.getByRole("button", { name: "Chat, 1 unread" })).toBeInTheDocument();
  rerender(view(2)); // the refreshed list already counts that message
  expect(screen.getByRole("button", { name: "Chat, 2 unread" })).toBeInTheDocument();
});

it("refreshes the session list when the chat opens, so a first open's room is known for its next message", async () => {
  const onAccessLost = vi.fn();
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), openSessionChat: vi.fn(async () => ({ roomId: "chat-1" })) } as unknown as SessionSource["client"];
  render(<SessionFocus focus={{ sessionId: "s1" }} entries={[{ session, ownerName: "Alice", own: false }]} hintRevision={0} client={client}
    resolveSessionOwner={resolveSessionOwner} onAccessLost={onAccessLost} onClose={vi.fn()} chatOpen onChatOpenChange={vi.fn()} />);
  await userEvent.click(await screen.findByRole("button", { name: "Open door" }));
  expect(client.openSessionChat).toHaveBeenCalledWith("s1");
  expect(onAccessLost).toHaveBeenCalled();
});

it("refreshes the session list when the open chat is read, so the header and the sidebar row drop the count", async () => {
  const onAccessLost = vi.fn();
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), openSessionChat: vi.fn() } as unknown as SessionSource["client"];
  render(<SessionFocus focus={{ sessionId: "s1" }} entries={[{ session: { ...session, chatRoomId: "chat-1", chatUnread: 1 }, ownerName: "Alice", own: false }]} hintRevision={0} client={client}
    resolveSessionOwner={resolveSessionOwner} onAccessLost={onAccessLost} onClose={vi.fn()} chatOpen onChatOpenChange={vi.fn()} />);
  await userEvent.click(await screen.findByRole("button", { name: "Read" }));
  expect(onAccessLost).toHaveBeenCalledTimes(1);
});

it("pushes the terminal aside while 420px of it stay, overlays below that, and takes the screen under 860px", () => {
  expect(sessionChatLayout(764, 1040)).toBe("push");
  expect(sessionChatLayout(763, 1040)).toBe("overlay");
  expect(sessionChatLayout(709, 984)).toBe("overlay"); // the acceptance window
  expect(sessionChatLayout(1200, 1500)).toBe("push");
  expect(sessionChatLayout(600, 859)).toBe("screen");
});

const rect = (top: number, bottom: number, width: number) => ({ top, bottom, width, height: bottom - top, left: 0, right: width, x: 0, y: top, toJSON: () => ({}) });
afterEach(() => { vi.restoreAllMocks(); });
function measured(panelWidth: number) {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.classList.contains("room-session-focus")) return rect(0, 600, panelWidth);
    if (this.classList.contains("session-state-panel")) return rect(120, 480, panelWidth);
    return rect(0, 0, 0);
  });
  // An ended session: its body is the state panel, between the status line and the resume strip.
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), openSessionChat: vi.fn() } as unknown as SessionSource["client"];
  render(<SessionFocus focus={{ sessionId: "s1" }} entries={[{ session: { ...session, status: "ended", endedAt: "2026-10-07T10:00:00Z" }, ownerName: "Alice", own: false }]} hintRevision={0} client={client}
    resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={vi.fn()} chatOpen onChatOpenChange={vi.fn()} />);
  return document.querySelector<HTMLElement>(".room-session-focus")!;
}

it("lays the chat out as a column when the panel is wide enough: nothing of the session is covered", () => {
  const panel = measured(900);
  expect(panel).toHaveClass("session-chat-open", "session-chat-push");
});

it("stops a narrow panel's overlay at the session's body, clear of the strips above and the controls below", () => {
  const panel = measured(709);
  expect(panel).toHaveClass("session-chat-open", "session-chat-overlay");
  expect(panel.style.getPropertyValue("--session-chat-top")).toBe("120px");
  expect(panel.style.getPropertyValue("--session-chat-bottom")).toBe("120px");
});

it("keeps the same panel mounted when its session leaves the list, until the read by id answers", async () => {
  // Prod 2026-10-08: a held session ended before its first event left the list; the panel had no session for
  // a moment, unmounted, and the viewer's terminal came back as a new one on "Connecting…".
  let answer!: (found: LocalSession) => void;
  const getLocalSession = vi.fn(() => new Promise<LocalSession>((resolve) => { answer = resolve; }));
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession, listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), requestLocalSessionPickup: vi.fn() } as unknown as SessionSource["client"];
  const view = (entries: { session: LocalSession; ownerName: string; own: boolean }[]) =>
    <SessionFocus focus={{ sessionId: "s1" }} entries={entries} hintRevision={0} client={client}
      resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={vi.fn()} />;
  const { rerender } = render(view([{ session, ownerName: "Alice", own: false }]));
  await screen.findByRole("heading", { name: /Load test/ });
  const mounted = document.querySelector(".session-status");
  expect(mounted).not.toBeNull();
  rerender(view([]));
  expect(screen.queryByText("Opening session…")).toBeNull();
  expect(document.querySelector(".session-status")).toBe(mounted);
  await act(async () => { answer({ ...session, status: "ended", terminal: undefined, wasHeld: true, endedAt: new Date().toISOString() }); });
  expect(getLocalSession).toHaveBeenCalledWith("s1");
  expect(document.querySelector(".session-status")).toBe(mounted);
  expect(statusLine()).toHaveTextContent("Ended");
  // A held session never claims nothing was sent: its first events may not have arrived.
  expect(screen.queryByText("This session ended before anything was sent.")).toBeNull();
});

it("lets the owner remove an ended session from the list, after a confirmation", async () => {
  const onRemove = vi.fn().mockResolvedValue(undefined);
  const ended = { ...session, status: "ended" as const, terminal: undefined, latestEventSeq: 3 };
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), requestLocalSessionPickup: vi.fn() } as unknown as SessionSource["client"];
  const view = (entry: { session: LocalSession; ownerName: string; own: boolean }) =>
    <SessionFocus focus={{ sessionId: "s1" }} entries={[entry]} hintRevision={0} client={client}
      resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={vi.fn()} onRemove={onRemove} />;
  // Not on someone else's, running or ended.
  const { rerender } = render(view({ session, ownerName: "Alice", own: false }));
  expect(screen.queryByRole("button", { name: /Remove/i })).toBeNull();
  rerender(view({ session: ended, ownerName: "Alice", own: false }));
  expect(screen.queryByRole("button", { name: /Remove/i })).toBeNull();
  rerender(view({ session: ended, ownerName: "You", own: true }));
  await userEvent.click(screen.getByRole("button", { name: "Remove Load test from the list" }));
  expect(onRemove).not.toHaveBeenCalled();
  await userEvent.click(within(screen.getByRole("dialog", { name: "Remove “Load test” from the list?" })).getByRole("button", { name: "Remove" }));
  expect(onRemove).toHaveBeenCalledWith("s1");
});

it("lets the owner end and remove a running session in one confirmed action, and says why when the server refuses", async () => {
  // Owner ruling 2026-10-08: remove ends it. The confirmation says it stops on the Mac.
  const onRemove = vi.fn().mockRejectedValueOnce(new ApiError(409, null, "This session's Mac is offline, so it cannot be stopped from here."))
    .mockResolvedValue(undefined);
  const onClose = vi.fn();
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), requestLocalSessionPickup: vi.fn() } as unknown as SessionSource["client"];
  const view = (shown: LocalSession) =>
    <SessionFocus focus={{ sessionId: "s1" }} entries={[{ session: shown, ownerName: "You", own: true }]} hintRevision={0} client={client}
      resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={onClose} onRemove={onRemove} />;
  // Not one End cannot stop: an offline Mac, or a Codex thread its app hosts.
  const { rerender } = render(view({ ...session, status: "offline" }));
  expect(screen.queryByRole("button", { name: /Remove/i })).toBeNull();
  rerender(view({ ...session, provider: "codex", terminal: undefined }));
  expect(screen.queryByRole("button", { name: /Remove/i })).toBeNull();
  rerender(view(session));
  expect(screen.queryByRole("button", { name: /from the list/ })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "End and remove Load test" }));
  const confirm = screen.getByRole("dialog", { name: "End and remove “Load test”?" });
  expect(confirm).toHaveTextContent("It stops on your Mac, and is removed for everyone in the workspace.");
  expect(onRemove).not.toHaveBeenCalled();
  await userEvent.click(within(confirm).getByRole("button", { name: "End and remove" }));
  expect(await within(confirm).findByRole("alert")).toHaveTextContent("This session's Mac is offline, so it cannot be stopped from here.");
  expect(onClose).not.toHaveBeenCalled();
  await userEvent.click(within(confirm).getByRole("button", { name: "End and remove" }));
  expect(onRemove).toHaveBeenCalledTimes(2);
  expect(onRemove).toHaveBeenLastCalledWith("s1");
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("keeps the panel open on a session its owner ended on purpose, though it left every list", async () => {
  // Owner ruling 2026-10-08: ended on purpose vanishes from the lists. The open panel reads it by id and keeps
  // the Ended state (and Resume) until it is closed; nothing closes it or remounts its body.
  const ended: LocalSession = { ...session, status: "ended", terminal: undefined, wasHeld: true, endedOnPurpose: true, latestEventSeq: 4,
    resumable: true, endedAt: new Date().toISOString() };
  const getLocalSession = vi.fn(async () => ended);
  const onClose = vi.fn();
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession, listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), requestLocalSessionPickup: vi.fn(),
    requestLocalSessionResume: vi.fn() } as unknown as SessionSource["client"];
  const view = (entries: { session: LocalSession; ownerName: string; own: boolean }[], hint = 0) =>
    <SessionFocus focus={{ sessionId: "s1" }} entries={entries} hintRevision={hint} client={client}
      resolveSessionOwner={() => ({ ownerName: "You", own: true })} onAccessLost={vi.fn()} onClose={onClose} onRemove={vi.fn()} />;
  const { rerender } = render(view([{ session, ownerName: "You", own: true }]));
  await screen.findByRole("heading", { name: /Load test/ });
  const mounted = document.querySelector(".session-status");
  // The owner pressed End: the next list no longer has the row.
  rerender(view([], 1));
  await act(async () => {});
  expect(getLocalSession).toHaveBeenCalledWith("s1");
  expect(document.querySelector(".session-status")).toBe(mounted);
  expect(statusLine()).toHaveTextContent("Ended");
  expect(screen.queryByText("This session is no longer shared.")).toBeNull();
  // Later list refreshes still lack it; the panel stays.
  rerender(view([], 2));
  await act(async () => {});
  expect(statusLine()).toHaveTextContent("Ended");
  expect(onClose).not.toHaveBeenCalled();
  // Still its owner's to remove for good.
  expect(screen.getByRole("button", { name: "Remove Load test from the list" })).toBeInTheDocument();
});

it("closes after a removal succeeded, never before, and for a displayed fork too", async () => {
  // The focus names the ended origin; the list shows its continuation (`forkedFrom`) in its place, under another id.
  const origin = { ...session, status: "ended" as const, terminal: undefined, latestEventSeq: 3 };
  const fork = { ...origin, sessionId: "s2", forkedFrom: "s1", title: "Load test" };
  const onRemove = vi.fn().mockRejectedValueOnce(new Error("409")).mockResolvedValue(undefined);
  const onClose = vi.fn();
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), requestLocalSessionPickup: vi.fn() } as unknown as SessionSource["client"];
  render(<SessionFocus focus={{ sessionId: "s1" }} entries={[{ session: origin, ownerName: "You", own: true }, { session: fork, ownerName: "You", own: true }]}
    hintRevision={0} client={client} resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={onClose} onRemove={onRemove} />);
  await userEvent.click(screen.getByRole("button", { name: "Remove Load test from the list" }));
  const confirm = screen.getByRole("dialog", { name: "Remove “Load test” from the list?" });
  await userEvent.click(within(confirm).getByRole("button", { name: "Remove" }));
  // Refused: the panel and its confirmation stay, and say so.
  expect(onRemove).toHaveBeenCalledWith("s2");
  expect(await within(confirm).findByRole("alert")).toHaveTextContent("Could not remove it. Try again.");
  expect(onClose).not.toHaveBeenCalled();
  await userEvent.click(within(confirm).getByRole("button", { name: "Remove" }));
  expect(onRemove).toHaveBeenCalledTimes(2);
  expect(onClose).toHaveBeenCalledTimes(1);
});

it("names a prompt sent by one of your own sessions as that session, not a teammate", async () => {
  const cursor: LocalSession = { ...session, sessionId: "cursor-1", provider: "cursor", title: "Cursor work" };
  const claude: LocalSession = { ...session, terminal: undefined, terminalReason: "plain", humanUserId: "alice", latestEventSeq: 1 };
  const events = [{ seq: 1, ts: "2026-10-22T10:00:00.000Z", kind: "text", role: "user", text: "ping from cursor", actor: { agentId: "cursor-1" } }];
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events, nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() } as unknown as SessionSource["client"];
  render(<SessionFocus focus={{ sessionId: "s1" }} entries={[{ session: claude, ownerName: "Alice", own: true }, { session: cursor, ownerName: "Alice", own: true }]} hintRevision={0} client={client}
    resolveSessionOwner={() => ({ ownerName: "Alice", own: true })} onAccessLost={vi.fn()} onClose={vi.fn()} />);
  expect(await screen.findByText(/Your Cursor/)).toBeInTheDocument();
  expect(screen.queryByText(/Teammate/)).toBeNull();
});
