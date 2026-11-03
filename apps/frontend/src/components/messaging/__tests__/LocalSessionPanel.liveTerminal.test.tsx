import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";
import { type LocalSession } from "@lightbulb/shared";
import { LocalSessionPanel } from "../LocalSessionPanel";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", orgId: "org_1" }) }));
// The terminal itself is tested in SessionTerminalView.test; here it just reports that it is live.
const typing = vi.hoisted(() => ({ canType: true, admitted: undefined as boolean | undefined, kept: undefined as boolean | undefined, mounts: 0, typed: [] as string[], calls: 0, failOn: [] as number[], failWith: "RATE_LIMITED" as string | null }));
vi.mock("../SessionTerminalView", async () => {
  const { useEffect } = await import("react");
  return { SessionTerminalView: ({ onLive, onCanType, admitted, kept, keys }: { onLive: (live: boolean) => void; onCanType?: (c: boolean) => void; admitted?: boolean; kept?: boolean; keys?: { current: ((d: string) => Promise<unknown>) | null } }) => {
    typing.admitted = admitted; typing.kept = kept;
    if (keys) keys.current = async (d) => { if (typing.failOn.includes(typing.calls++)) { const { ApiError } = await import("@lightbulb/shared"); throw typing.failWith ? new ApiError(409, { code: typing.failWith }, "refused") : new TypeError("Failed to fetch"); } typing.typed.push(d); };
    useEffect(() => { typing.mounts += 1; }, []);
    useEffect(() => { onLive(true); onCanType?.(typing.canType); }, [onLive, onCanType]);
    return <div role="region" aria-label="Live terminal (view only)"><div className="session-terminal-scroll" /></div>;
  } };
});

vi.mock("../SessionBrowserTab", async importOriginal => ({
  ...await importOriginal<typeof import("../SessionBrowserTab")>(),
  SessionBrowserTab: ({ canType }: { canType: boolean }) => <div role="region" aria-label="Session browser">{canType ? "typing" : "read-only"}</div>,
}));

const session: LocalSession = { sessionId: "s", deviceId: "mac", humanUserId: "alice", provider: "claude", terminal: "held", generation: "g1", status: "idle", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };
const client = { listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSession: vi.fn(), listWorkspaceSessions: vi.fn(), setLocalSessionSharing: vi.fn(), watchLocalSession: vi.fn(async () => undefined), sendLocalSessionInput: vi.fn() };
const below = (a: HTMLElement, b: HTMLElement) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

it("keeps saved conversation readable after a terminal exits, without stale control warnings", async () => {
  client.listLocalSessionEvents.mockResolvedValueOnce({ events: [
    { seq: 1, kind: "text", truncated: false, role: "assistant", text: "Your orchestration notes are still here." },
  ], nextAfterSeq: null } as never);
  render(<LocalSessionPanel session={{ ...session, status: "ended", latestEventSeq: 1, controlUnavailableReason: "history-gap" }}
    ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} />);
  expect(await screen.findByText("Your orchestration notes are still here.")).toBeInTheDocument();
  expect(screen.queryByText(/Control is unavailable/)).toBeNull();
  expect(screen.queryByText(/screen is gone/)).toBeNull();
});

it("finds this Mac's terminal even before the relay advertises it", async () => {
  const invoke = vi.fn(async (command: string) => command === "terminal_panel" ? { live: true } : null);
  Object.defineProperty(window, "__TAURI__", { configurable: true, value: { core: { invoke } } });
  try {
    render(<LocalSessionPanel session={{ ...session, terminal: undefined, terminalReason: "plain" }}
      ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} />);
    expect(await screen.findByRole("region", { name: "Session terminal" })).toBeInTheDocument();
    expect(invoke).toHaveBeenCalledWith("terminal_panel", expect.objectContaining({ sessionId: "s" }));
    expect(screen.queryByRole("textbox", { name: /send to/i })).toBeNull();
  } finally { Reflect.deleteProperty(window, "__TAURI__"); }
});

it("does not offer an ordinary follow-up while a terminal question needs an answer", async () => {
  render(<LocalSessionPanel session={{ ...session, terminal: undefined, terminalReason: "plain", status: "waiting-approval", waitingFor: "Which approach?" }}
    ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} />);
  expect(await screen.findByText("Which approach?")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /send to/i })).toBeNull();
  expect(document.querySelector(".session-live-hint")).toBeNull();
});

it("shows a control connection warning once", () => {
  render(<LocalSessionPanel session={{ ...session, controlUnavailableReason: "history-gap" }}
    ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} />);
  expect(screen.getAllByText(/History has a gap/)).toHaveLength(1);
});

it("drops the message box and the history region when the viewer can type in the live terminal", async () => {
  render(<LocalSessionPanel session={session} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(screen.queryByRole("region", { name: "Native session history" })).toBeNull();
  expect(screen.queryByRole("textbox", { name: /send to/i })).toBeNull();
});

it("brings the message box back when the Mac refuses typing in the terminal", async () => {
  typing.canType = false;
  render(<LocalSessionPanel session={session} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(await screen.findByRole("textbox", { name: /send to/i })).toBeTruthy();
  typing.canType = true;
});

it("keeps the message box on a transcript", () => {
  render(<LocalSessionPanel session={{ ...session, status: "offline" }} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} />);
  expect(screen.getByRole("textbox", { name: /send to/i })).toBeTruthy();
});

it("shows Watching · Only Alice can type, no text box and no Ask to join, under a live terminal for a teammate who may not send", async () => {
  render(<LocalSessionPanel session={session} ownerName="Alice" own={false} client={{ ...client, requestLocalSessionJoin: vi.fn() } as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  const terminal = await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(below(terminal, await screen.findByText(/Watching · Only Alice can type in this session/))).toBe(true);
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Ask to join" })).toBeNull();
});

it("never labels the status line as a transcript, and shows the ended and offline panels", () => {
  const props = { ownerName: "Alice", client: client as never, revision: 0, onAccessLost: () => {} };
  const ended = render(<LocalSessionPanel {...props} own={false} session={{ ...session, status: "ended", latestEventSeq: 3, endedAt: new Date(Date.now() - 3600_000).toISOString() }} />);
  expect(document.querySelector(".session-status-line")!.textContent).not.toMatch(/Transcript/);
  expect(document.querySelector(".session-state-title")!.textContent).toMatch(/^Ended/);
  expect(screen.getByText("The screen is gone: this session's terminal closed.")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Native session history" })).toBeNull();
  ended.unmount();
  const offline = render(<LocalSessionPanel {...props} own session={{ ...session, status: "offline", lastEventAt: new Date(Date.now() - 7200_000).toISOString() }} />);
  expect(screen.getByText("Your Mac is offline")).toBeTruthy();
  expect(screen.getByText(/Last seen/)).toBeTruthy();
  expect(screen.getByText("Mac offline. Follow-ups wait up to 24 hours for this session to reconnect.")).toBeTruthy();
  expect(document.querySelector(".session-status-line")!.textContent).not.toMatch(/Transcript/);
  offline.unmount();
  // Paused: the Mac stops heartbeating it, which is not the Mac being offline.
  const paused = render(<LocalSessionPanel {...props} own session={{ ...session, status: "offline", shared: false }} />);
  expect(document.querySelector(".session-state-title")!.textContent).toBe("Paused");
  expect(screen.queryByText("Your Mac is offline")).toBeNull();
  paused.unmount();
  render(<LocalSessionPanel {...props} own={false} session={session} />);
  expect(screen.queryByText(/Transcript/)).toBeNull();
});

it("says to answer in the live terminal only while the terminal accepts typing", async () => {
  const waiting = { ...session, status: "waiting-approval" as const };
  const props = { ownerName: "Alice", own: true, client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const { unmount } = render(<LocalSessionPanel {...props} session={waiting} />);
  expect(await screen.findByText("Answer it in the live terminal below.")).toBeTruthy();
  unmount();
  typing.canType = false;
  render(<LocalSessionPanel {...props} session={waiting} />);
  expect(await screen.findByText(/this prompt can't be answered from here/)).toBeTruthy();
  expect(screen.queryByText("Answer it in the live terminal below.")).toBeNull();
  typing.canType = true;
});

// A running session with no terminal shows its conversation under one hint line, as the phone does.
it("shows the conversation, not a wall, for a plain session", async () => {
  client.watchLocalSession.mockClear();
  client.listLocalSessionEvents.mockResolvedValueOnce({ events: [
    { seq: 1, truncated: false, kind: "text", role: "user", text: "fix it" },
    { seq: 2, truncated: false, kind: "text", role: "assistant", text: "Done." }], nextAfterSeq: null } as never);
  const onMessageOwner = vi.fn();
  const props = { ownerName: "Alice", client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {}, onMessageOwner };
  const plain = { ...session, terminal: undefined, terminalReason: "plain" as const, status: "busy" as const, latestEventSeq: 2 };
  const mine = render(<LocalSessionPanel {...props} own session={plain} />);
  expect(await screen.findByRole("region", { name: "Native session history" })).toBeTruthy();
  expect(await screen.findByText("fix it")).toBeTruthy();
  expect(await screen.findByText("Done.")).toBeTruthy();
  expect(client.watchLocalSession).not.toHaveBeenCalled();
  expect(screen.queryByText(/not started through Lightbulb/)).toBeNull();
  expect(document.querySelector(".session-state-panel")).toBeNull();
  expect(document.querySelector(".session-live-hint")).toBeNull();
  expect(screen.getByRole("textbox", { name: /send to/i })).toBeTruthy();
  mine.unmount();
  const codex = render(<LocalSessionPanel {...props} own session={{ ...plain, provider: "codex", terminalReason: "no-daemon" }} />);
  expect(document.querySelector(".session-live-hint")).toBeNull();
  codex.unmount();
  render(<LocalSessionPanel {...props} own={false} session={plain} />);
  expect(document.querySelector(".session-live-hint")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Message Alice" }));
  expect(onMessageOwner).toHaveBeenCalled();
});

it("shows the conversation of a session that runs inside an app, with one line saying why there is no terminal", async () => {
  client.watchLocalSession.mockClear();
  const events = { events: [
    { seq: 1, truncated: false, kind: "text", role: "user", text: "run it" },
    { seq: 2, truncated: false, kind: "text", role: "assistant", text: "All good." },
  ], nextAfterSeq: null };
  client.listLocalSessionEvents.mockResolvedValue(events as never);
  const app: LocalSession = { ...session, terminal: undefined, terminalReason: "unknown", latestEventSeq: 2 };
  const props = { ownerName: "Alice", client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const theirs = render(<LocalSessionPanel {...props} own={false} session={app} />);
  const log = await screen.findByRole("log", { name: "Session conversation" });
  expect(await within(log).findByText("All good.")).toBeTruthy();
  expect(within(log).getByText("run it")).toBeTruthy();
  expect(screen.getByRole("note").textContent).toBe("This session runs inside an app, so there is no terminal to show. This is its conversation.");
  expect(document.querySelector(".session-state-panel")).toBeNull();
  expect(client.watchLocalSession).not.toHaveBeenCalled();
  expect(screen.queryByRole("textbox")).toBeNull(); // a teammate who may not send reads it
  theirs.unmount();
  // Its owner keeps the follow-up box; ended or offline, the conversation stays.
  const mine = render(<LocalSessionPanel {...props} own session={app} />);
  expect(screen.getByRole("textbox", { name: /send to/i })).toBeTruthy();
  mine.unmount();
  render(<LocalSessionPanel {...props} own={false} session={{ ...app, status: "ended" }} />);
  expect(await within(await screen.findByRole("log")).findByText("All good.")).toBeTruthy();
  expect(document.querySelector(".session-ended-line")!.textContent).toBe("Ended");
  expect(screen.queryByText(/screen is gone/)).toBeNull();
  cleanup();
  // Offline: the offline panel (last seen, Resume, Pick up) stays above the conversation.
  render(<LocalSessionPanel {...props} own={false} session={{ ...app, status: "offline" }} />);
  expect(screen.getByText("Alice's Mac is offline")).toBeTruthy();
  expect(await within(await screen.findByRole("log")).findByText("All good.")).toBeTruthy();
  cleanup();
  // Ended with nothing in it: the ended panel alone.
  render(<LocalSessionPanel {...props} own={false} session={{ ...app, provider: "codex", status: "ended", latestEventSeq: 0 }} />);
  expect(screen.getByText("This session ended before anything was sent.")).toBeTruthy();
  expect(screen.queryByRole("log")).toBeNull();
  expect(screen.queryByRole("note")).toBeNull();
  cleanup();
  // Its events arrived after the end, and the cached row still says none: the conversation shows once they are read.
  client.listLocalSessionEvents.mockResolvedValue(events as never);
  render(<LocalSessionPanel {...props} own={false} session={{ ...app, status: "ended", latestEventSeq: 0 }} />);
  expect(await within(await screen.findByRole("log")).findByText("All good.")).toBeTruthy();
  expect(screen.queryByText("This session ended before anything was sent.")).toBeNull();
  client.listLocalSessionEvents.mockResolvedValue({ events: [], nextAfterSeq: null } as never);
});

it("probes this Mac's holder for offline sessions and running sessions before relay metadata catches up", () => {
  const invoke = vi.fn(async () => ({ live: false }));
  (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
  try {
    const props = { ownerName: "Alice", own: true, client: client as never, revision: 0, onAccessLost: () => {} };
    const offline = render(<LocalSessionPanel {...props} session={{ ...session, status: "offline" }} />);
    expect(invoke).toHaveBeenCalledWith("terminal_panel", expect.objectContaining({ sessionId: "s" }));
    offline.unmount();
    invoke.mockClear();
    render(<LocalSessionPanel {...props} session={{ ...session, terminal: undefined, status: "busy" }} />);
    expect(invoke).toHaveBeenCalledWith("terminal_panel", expect.objectContaining({ sessionId: "s" }));
  } finally {
    delete (window as { __TAURI__?: unknown }).__TAURI__;
  }
});

it("says who watches over the owner's own terminal on this Mac, from the app, as the web terminal does", async () => {
  const invoke = vi.fn(async (command: string) => command === "terminal_panel" ? { live: true } : { watching: 1, viaLink: 2 });
  (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
  try {
    render(<LocalSessionPanel session={session} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
    expect(await screen.findByText("1 watching · 2 via link")).toBeTruthy();
    expect(invoke).toHaveBeenCalledWith("local_session_audience", { sessionId: "s", generation: "g1" });
  } finally {
    delete (window as { __TAURI__?: unknown }).__TAURI__;
  }
});

it("reads the origin's history for a moved session that has had no turn of its own", async () => {
  const listLocalSessionEvents = vi.fn(async () => ({ events: [], nextAfterSeq: null }));
  const getLocalSession = vi.fn(async () => ({ ...session, sessionId: "origin", status: "ended", latestEventSeq: 120 }));
  render(<LocalSessionPanel session={{ ...session, status: "offline", forkedFrom: "origin" }} ownerName="Alice" own
    client={{ ...client, listLocalSessionEvents, getLocalSession } as never} revision={0} onAccessLost={() => {}} />);
  // The newest page of the ORIGIN's 120 events, not the moved session's empty history.
  await waitFor(() => expect(listLocalSessionEvents).toHaveBeenCalledWith("origin", 70, 50));
});

it("moves a moved session's transcript to its own events on the first hint after its first turn", async () => {
  const listLocalSessionEvents = vi.fn(async () => ({ events: [], nextAfterSeq: null }));
  const moved = { ...session, status: "offline" as const, forkedFrom: "origin" };
  // The directory row still says 0 events (it trails transcript hints); after the turn the session itself has 40.
  let turned = false;
  const getLocalSession = vi.fn(async (id: string) => id === "origin" ? { ...session, sessionId: "origin", latestEventSeq: 120 } : { ...moved, latestEventSeq: turned ? 40 : 0 });
  const props = { session: moved, ownerName: "Alice", own: true, client: { ...client, listLocalSessionEvents, getLocalSession } as never, onAccessLost: () => {} };
  const view = render(<LocalSessionPanel {...props} revision={0} />);
  await waitFor(() => expect(listLocalSessionEvents).toHaveBeenCalledWith("origin", 70, 50));
  // The first read, before the origin resolved, was the session's own (empty) history: start the count here.
  listLocalSessionEvents.mockClear();
  turned = true;
  view.rerender(<LocalSessionPanel {...props} revision={1} />);
  await waitFor(() => expect(listLocalSessionEvents).toHaveBeenCalledWith("s", 0, 50));
});

it("reads a moved session's own events when it is opened after its first turn but before the directory catches up", async () => {
  const listLocalSessionEvents = vi.fn(async () => ({ events: [], nextAfterSeq: null }));
  const moved = { ...session, status: "offline" as const, forkedFrom: "origin" };
  const getLocalSession = vi.fn(async (id: string) => id === "origin" ? { ...session, sessionId: "origin", latestEventSeq: 120 } : { ...moved, latestEventSeq: 40 });
  render(<LocalSessionPanel session={moved} ownerName="Alice" own client={{ ...client, listLocalSessionEvents, getLocalSession } as never} revision={7} onAccessLost={() => {}} />);
  await waitFor(() => expect(listLocalSessionEvents).toHaveBeenLastCalledWith("s", 0, 50));
});

it("retries a moved session's origin on the next hint after a failed request", async () => {
  const listLocalSessionEvents = vi.fn(async () => ({ events: [], nextAfterSeq: null }));
  const moved = { ...session, status: "offline" as const, forkedFrom: "origin" };
  let failing = true;
  const getLocalSession = vi.fn(async (id: string) => {
    if (id !== "origin") return moved;
    if (failing) throw new Error("network");
    return { ...session, sessionId: "origin", latestEventSeq: 120 };
  });
  const props = { session: moved, ownerName: "Alice", own: true, client: { ...client, listLocalSessionEvents, getLocalSession } as never, onAccessLost: () => {} };
  const view = render(<LocalSessionPanel {...props} revision={0} />);
  await waitFor(() => expect(getLocalSession).toHaveBeenCalledWith("origin"));
  failing = false;
  view.rerender(<LocalSessionPanel {...props} revision={1} />);
  await waitFor(() => expect(listLocalSessionEvents).toHaveBeenCalledWith("origin", 70, 50));
});

it("says an empty ended session never started, and offers Resume only for one that has history", async () => {
  // The server answers `resumable`: false for a session that ended before its first message.
  const props = { ownerName: "Alice", own: true, client: { ...client, resumeLocalSession: vi.fn() } as never, revision: 0, onAccessLost: () => {} };
  const empty = render(<LocalSessionPanel {...props} session={{ ...session, status: "ended", latestEventSeq: 0, resumable: false }} />);
  expect(await screen.findByText("This session ended before anything was sent.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /^Resume/ })).toBeNull();
  empty.unmount();
  // History the Mac could not read may hold a real conversation: no "nothing was sent" claim beside its Resume.
  const unread = render(<LocalSessionPanel {...props} session={{ ...session, status: "ended", latestEventSeq: 0, controlUnavailableReason: "history-gap", resumable: true }} />);
  expect(await screen.findByText("The screen is gone: this session's terminal closed.")).toBeTruthy();
  expect(screen.queryByText("This session ended before anything was sent.")).toBeNull();
  unread.unmount();
  render(<LocalSessionPanel {...props} session={{ ...session, status: "ended", latestEventSeq: 4, resumable: true }} />);
  expect(await screen.findByRole("button", { name: "Continue" })).toBeTruthy();
});

it("pauses an admitted teammate's typing while the owner answers a join request", async () => {
  render(<LocalSessionPanel session={{ ...session, allowedSenders: ["alice"], inputPaused: true }} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  expect(await screen.findByText(/Paused while Alice answers a request to join/)).toBeTruthy();
  expect(screen.getByRole("region", { name: "Live terminal (view only)" })).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Ask to join" })).toBeNull();
});

// A newer Mac (bandOwnerOnly) keeps typing open while a request waits: the server sends no inputPaused.
it("keeps an admitted teammate typing when the owner's Mac does not pause", async () => {
  render(<LocalSessionPanel session={{ ...session, allowedSenders: ["alice"], inputPaused: false }} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(typing.admitted).toBe(true);
  expect(screen.queryByText(/Paused while/)).toBeNull();
});

it("shows no message box while paused even when the terminal refuses typing", async () => {
  typing.canType = false;
  try {
    render(<LocalSessionPanel session={{ ...session, allowedSenders: ["alice"], inputPaused: true }} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
    expect(await screen.findByText(/Paused while Alice answers/)).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
  } finally { typing.canType = true; }
});

it("shows no transcript while a held session's terminal has no frame yet", () => {
  // The real SessionTerminalView draws Connecting…/Reconnecting… (SessionTerminalView.test); the panel never swaps to rows.
  render(<LocalSessionPanel session={{ ...session, status: "busy" }} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  expect(screen.queryByRole("region", { name: "Native session history" })).toBeNull();
  expect(document.querySelector(".session-status-line")!.textContent).not.toMatch(/Transcript/);
});

it("says the login session restarted and offers Resume", async () => {
  const props = { ownerName: "Alice", own: true, client: { ...client, resumeLocalSession: vi.fn() } as never, revision: 0, onAccessLost: () => {} };
  render(<LocalSessionPanel {...props} session={{ ...session, status: "ended", latestEventSeq: 3, resumable: true, endedReason: "login-session-ended" }} />);
  expect(document.querySelector(".session-state-title, .session-ended-line")?.textContent).toBe("Ended · your Mac's login session restarted");
  expect(await screen.findByRole("button", { name: /^Continue/ })).toBeTruthy();
});

const textEvent = (seq: number) => ({ seq, kind: "text", role: "user", text: `t${seq}` });
const change = { seq: 3, kind: "tool", role: "assistant", itemId: "c1", tool: { name: "FileChange", status: "completed", result: { "a.ts": { type: "add", content: "x\n" } } } };

it("keeps loading older pages without a scroll so the plan and changed files see the whole session", async () => {
  const list = vi.fn(async (_id: string, after: number) => ({ events: after === 0 ? [textEvent(1), textEvent(2), change, ...Array.from({ length: 7 }, (_, i) => textEvent(4 + i))] : Array.from({ length: 50 }, (_, i) => textEvent(11 + i)), nextAfterSeq: null }));
  render(<LocalSessionPanel session={{ ...session, status: "idle", latestEventSeq: 60 }} ownerName="Alice" own client={{ ...client, listLocalSessionEvents: list } as never} revision={0} onAccessLost={() => {}} />);
  await waitFor(() => expect(list).toHaveBeenCalledWith("s", 0, 10));
  expect(await screen.findByText(/1 file changed/)).toBeTruthy();
});

it("keeps the reader's latest scroll position when an older page arrives during live work", async () => {
  let height = 1000;
  const positions = new WeakMap<HTMLElement, number>();
  const heightSpy = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(() => height);
  const viewportSpy = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(200);
  Object.defineProperty(HTMLElement.prototype, "scrollTop", { configurable: true,
    get() { return positions.get(this) ?? 400; }, set(value: number) { positions.set(this, value); } });
  let releaseOlder!: (page: { events: ReturnType<typeof textEvent>[]; nextAfterSeq: null }) => void;
  const older = new Promise<{ events: ReturnType<typeof textEvent>[]; nextAfterSeq: null }>(resolve => { releaseOlder = resolve; });
  const list = vi.fn(async (_id: string, after: number) => after === 0 ? older
    : { events: Array.from({ length: 50 }, (_, i) => textEvent(11 + i)), nextAfterSeq: null });
  try {
    render(<LocalSessionPanel session={{ ...session, provider: "codex", terminal: undefined, terminalReason: "plain", status: "busy", latestEventSeq: 60 }}
      ownerName="Alice" own client={{ ...client, listLocalSessionEvents: list } as never} revision={0} onAccessLost={() => {}} />);
    await waitFor(() => expect(list).toHaveBeenCalledWith("s", 0, 10));
    const history = screen.getByRole("region", { name: "Native session history" });
    history.scrollTop = 650;
    fireEvent.scroll(history);
    height = 1200;
    await act(async () => releaseOlder({ events: Array.from({ length: 10 }, (_, i) => textEvent(i + 1)), nextAfterSeq: null }));
    expect(history.scrollTop).toBe(850);
  } finally {
    cleanup(); heightSpy.mockRestore(); viewportSpy.mockRestore();
    Reflect.deleteProperty(HTMLElement.prototype, "scrollTop");
  }
});

it("does not snap a reader back to the tail when new output grows the history", async () => {
  let height = 1000;
  const heightSpy = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(() => height);
  const viewportSpy = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(200);
  const resize = new Map<Element, () => void>();
  const Observer = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class {
    constructor(private callback: ResizeObserverCallback) {}
    observe(node: Element) { resize.set(node, () => this.callback([], this as unknown as ResizeObserver)); }
    disconnect() {} unobserve() {}
  } as unknown as typeof ResizeObserver;
  let appended = false;
  const list = vi.fn(async (_id: string, after: number) => ({ events: after === 0 ? [textEvent(1), textEvent(2)] : appended && after === 2 ? [textEvent(3)] : [], nextAfterSeq: null }));
  const props = { ownerName: "Alice", own: true, client: { ...client, listLocalSessionEvents: list } as never, onAccessLost: () => {} };
  const running = { ...session, provider: "codex" as const, terminal: undefined, terminalReason: "plain" as const, status: "busy" as const, latestEventSeq: 2 };
  try {
    const view = render(<LocalSessionPanel {...props} session={running} revision={0} />);
    await screen.findByText("t2");
    const history = screen.getByRole("region", { name: "Native session history" });
    history.scrollTop = 250; fireEvent.scroll(history);
    appended = true; height = 1400;
    view.rerender(<LocalSessionPanel {...props} session={{ ...running, latestEventSeq: 3 }} revision={1} />);
    await screen.findByText("t3");
    act(() => resize.get(history.firstElementChild!)?.());
    expect(history.scrollTop).toBe(250);
  } finally {
    cleanup(); heightSpy.mockRestore(); viewportSpy.mockRestore(); globalThis.ResizeObserver = Observer;
  }
});

it("shows a retry line in the status area when the first history load fails, and clears it after a good retry", async () => {
  let fail = true;
  const list = vi.fn(async () => { if (fail) throw new Error("net"); return { events: [], nextAfterSeq: null }; });
  render(<LocalSessionPanel session={{ ...session, status: "idle" }} ownerName="Alice" own client={{ ...client, listLocalSessionEvents: list } as never} revision={0} onAccessLost={() => {}} />);
  expect(await screen.findByText("Could not load session details.")).toBeTruthy();
  fail = false;
  screen.getByRole("button", { name: "Retry" }).click();
  await waitFor(() => expect(screen.queryByText("Could not load session details.")).toBeNull());
  expect(list.mock.calls.length).toBeGreaterThanOrEqual(2);
});

it("keeps a queued follow-up visible and cancellable after the session ends", async () => {
  const receipt = { requestId: "r1", expectedGeneration: "g1", kind: "send", text: "queued while offline", state: "pending", deliverBy: Math.floor(Date.now() / 1000) + 3600 };
  const cancel = vi.fn(async () => ({ ...receipt, state: "cancelled" }));
  render(<LocalSessionPanel session={{ ...session, status: "ended", latestEventSeq: 3 }} ownerName="Alice" own
    client={{ ...client, listLocalSessionOutbox: vi.fn(async () => ({ commands: [receipt] })), cancelLocalSessionCommand: cancel } as never} revision={0} onAccessLost={() => {}} />);
  expect(await screen.findByText("queued while offline")).toBeTruthy();
  screen.getByRole("button", { name: /cancel/i }).click();
  await waitFor(() => expect(cancel).toHaveBeenCalledWith("s", "r1"));
});

it("reads 20 older pages in the background and no more, Details open or not", async () => {
  const list = vi.fn(async (_id: string, after: number, limit: number) => ({ events: Array.from({ length: limit }, (_, i) => textEvent(after + 1 + i)), nextAfterSeq: null }));
  render(<LocalSessionPanel session={{ ...session, status: "idle", latestEventSeq: 1300 }} ownerName="Alice" own client={{ ...client, listLocalSessionEvents: list } as never} revision={0} onAccessLost={() => {}} />);
  await waitFor(() => expect(list).toHaveBeenCalledTimes(21));
  await new Promise(r => setTimeout(r, 100));
  expect(list).toHaveBeenCalledTimes(21);
  screen.getByRole("button", { name: "Details" }).click();
  await new Promise(r => setTimeout(r, 150));
  expect(list).toHaveBeenCalledTimes(21);
});

it("keeps an ended held session's final screen, read-only, under an Ended line with Resume", async () => {
  const ended: LocalSession = { ...session, status: "ended", screenAvailable: true, latestEventSeq: 4, resumable: true, endedAt: new Date(Date.now() - 120_000).toISOString() };
  render(<LocalSessionPanel session={ended} ownerName="Alice" own client={{ ...client, resumeLocalSession: vi.fn() } as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(typing.admitted).toBe(false);
  expect(document.querySelector(".session-ended-line")!.textContent).toMatch(/^Ended · /);
  expect(screen.queryByText("The screen is gone: this session's terminal closed.")).toBeNull();
  expect(await screen.findByRole("button", { name: "Continue" })).toBeTruthy();
});

it("says the screen is gone once the Mac no longer shows it", () => {
  render(<LocalSessionPanel session={{ ...session, status: "ended", latestEventSeq: 4 }} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  expect(screen.getByText("The screen is gone: this session's terminal closed.")).toBeTruthy();
  expect(screen.queryByRole("region", { name: /Live terminal/ })).toBeNull();
});

it("offers Pick up and a disabled Resume while the owner's Mac is offline", () => {
  const offline: LocalSession = { ...session, status: "offline", pickup: { allowed: true, state: "ready", hasCode: false } };
  render(<LocalSessionPanel session={offline} ownerName="Alice" own={false} client={{ ...client, requestLocalSessionPickup: vi.fn() } as never} revision={0} onAccessLost={() => {}} />);
  expect(screen.getByText("Alice's Mac is offline")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();
  expect(screen.getByText("Resume works once Alice's Mac is back. Pick up continues it on your Mac now.")).toBeTruthy();
  expect(screen.getByRole("button", { name: /Pick up/ })).toBeTruthy();
});

it("says Ended just now, not that the screen is gone, while a held session's kept screen is still on its way", async () => {
  // Claude's SessionEnd hook ends the session before the provider exits; the Mac reports the kept screen on a later heartbeat.
  vi.useFakeTimers();
  const gone = "The screen is gone: this session's terminal closed.";
  const ended: LocalSession = { ...session, status: "ended", latestEventSeq: 4, endedAt: new Date(Date.now() - 2_000).toISOString() };
  const view = render(<LocalSessionPanel session={ended} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  expect(screen.getByText("Ended just now")).toBeTruthy();
  expect(screen.queryByText(gone)).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(17_000); });
  expect(screen.queryByText(gone)).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(1_100); });
  expect(screen.getByText(gone)).toBeTruthy();
  expect(screen.queryByText("Ended just now")).toBeNull();
  view.unmount();
  // Opened before the Mac's first heartbeat after the exit: the server sends no terminal for an ended session without a screen, only that it was held.
  const early = render(<LocalSessionPanel session={{ ...ended, terminal: undefined, wasHeld: true, endedAt: new Date().toISOString() }} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  expect(screen.getByText("Ended just now")).toBeTruthy();
  expect(screen.queryByText(gone)).toBeNull();
  early.unmount();
  // Never held: there is no screen to wait for.
  render(<LocalSessionPanel session={{ ...ended, terminal: undefined, endedAt: new Date().toISOString() }} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  expect(screen.getByText(gone)).toBeTruthy();
  vi.useRealTimers();
});

it("opens an ended session's kept screen at its top left, once, and leaves a running one at its tail", async () => {
  const ended: LocalSession = { ...session, status: "ended", screenAvailable: true, latestEventSeq: 4, endedAt: new Date(Date.now() - 120_000).toISOString() };
  const view = render(<LocalSessionPanel session={ended} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  const box = document.querySelector<HTMLElement>(".session-terminal-scroll")!;
  // The terminal view follows its last row; in an ended picture that can be a blank part of the screen.
  box.scrollTop = 400; box.scrollLeft = 30;
  fireEvent.scroll(box);
  expect([box.scrollTop, box.scrollLeft]).toEqual([0, 0]);
  box.scrollTop = 200;
  fireEvent.scroll(box);
  expect(box.scrollTop).toBe(200); // the reader's own scrolling is left alone
  view.unmount();
  render(<LocalSessionPanel session={session} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  const live = document.querySelector<HTMLElement>(".session-terminal-scroll")!;
  live.scrollTop = 400;
  fireEvent.scroll(live);
  expect(live.scrollTop).toBe(400);
});

it("keeps the terminal a viewer was watching mounted from the end to the kept screen", async () => {
  // Prod 2026-10-07 (web d829499b5): the row is ended for seconds before the Mac's heartbeat reports the kept screen,
  // and the server sends no `terminal` meanwhile. The view was unmounted and a new one mounted on "Connecting…".
  vi.useFakeTimers();
  typing.mounts = 0;
  const props = { ownerName: "Alice", own: false, client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const terminal = () => screen.queryByRole("region", { name: "Live terminal (view only)" });
  const view = render(<LocalSessionPanel {...props} session={session} />);
  expect(terminal()).toBeTruthy();
  const ended: LocalSession = { ...session, status: "ended", terminal: undefined, wasHeld: true, latestEventSeq: 4, endedAt: new Date().toISOString() };
  view.rerender(<LocalSessionPanel {...props} session={ended} />);
  expect(terminal()).toBeTruthy();
  expect(document.querySelector(".session-ended-line")!.textContent).toMatch(/^Ended · /);
  expect(screen.queryByText("Ended just now")).toBeNull();
  expect([typing.admitted, typing.kept]).toEqual([false, false]); // read-only, and the kept picture is not there yet
  await act(async () => { await vi.advanceTimersByTimeAsync(9_000); });
  view.rerender(<LocalSessionPanel {...props} session={{ ...ended, terminal: "held", screenAvailable: true }} />);
  expect(terminal()).toBeTruthy();
  expect(typing.kept).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(terminal()).toBeTruthy();
  expect(typing.mounts).toBe(1); // one terminal the whole time: its picture was never thrown away
  vi.useRealTimers();
});

it("gives up the terminal a viewer was watching when no kept screen is reported in 20 s", async () => {
  vi.useFakeTimers();
  const props = { ownerName: "Alice", own: false, client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const view = render(<LocalSessionPanel {...props} session={session} />);
  view.rerender(<LocalSessionPanel {...props} session={{ ...session, status: "ended", terminal: undefined, wasHeld: true, latestEventSeq: 4, endedAt: new Date().toISOString() }} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(19_000); });
  expect(screen.queryByRole("region", { name: "Live terminal (view only)" })).toBeTruthy();
  await act(async () => { await vi.advanceTimersByTimeAsync(1_100); });
  expect(screen.queryByRole("region", { name: "Live terminal (view only)" })).toBeNull();
  expect(screen.getByText("The screen is gone: this session's terminal closed.")).toBeTruthy();
  vi.useRealTimers();
});

it("offers Open in a terminal on a running app session to its owner only", () => {
  const app = { ...session, terminal: undefined, terminalReason: "app" as const };
  const panel = (s: LocalSession, own: boolean) => render(<LocalSessionPanel session={s} ownerName="Alice" own={own} client={client as never} revision={0} onAccessLost={() => {}} />);
  panel(app, true);
  expect(screen.getByRole("button", { name: "Open in a terminal" })).toBeTruthy();
  expect(screen.getByText("Opens this conversation in a live terminal on your Mac. The app keeps its copy.")).toBeTruthy();
  cleanup();
  panel(app, false);
  expect(screen.queryByRole("button", { name: "Open in a terminal" })).toBeNull();
  cleanup();
  panel({ ...app, status: "ended" }, true);
  expect(screen.queryByRole("button", { name: "Open in a terminal" })).toBeNull();
  cleanup();
  panel(session, true);
  expect(screen.queryByRole("button", { name: "Open in a terminal" })).toBeNull();
});

it("keeps the Browser tab read-only while the owner has paused others' input, and resets to Terminal when browsing stops", async () => {
  const props = { ownerName: "Alice", own: false, client: { ...client, requestLocalSessionJoin: vi.fn() } as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const browsing = { ...session, browser: "mac", teamControl: true } as LocalSession;
  const view = render(<LocalSessionPanel {...props} session={browsing} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  fireEvent.click(screen.getByRole("tab", { name: "Browser" }));
  expect((await screen.findByRole("region", { name: "Session browser" })).textContent).toBe("typing");
  view.rerender(<LocalSessionPanel {...props} session={{ ...browsing, inputPaused: true }} />);
  expect(screen.getByRole("region", { name: "Session browser" }).textContent).toBe("read-only");
  view.rerender(<LocalSessionPanel {...props} session={{ ...browsing, browser: null }} />);
  view.rerender(<LocalSessionPanel {...props} session={browsing} />);
  expect(screen.queryByRole("region", { name: "Session browser" })).toBeNull();
});

it("offers Open in a terminal for an app session, never for a Cursor or Grok CLI session", () => {
  const app = { ...session, terminal: undefined, terminalReason: "app" as const };
  const props = { ownerName: "Alice", own: true, client: client as never, revision: 0, onAccessLost: () => {} };
  const claude = render(<LocalSessionPanel {...props} session={app} />);
  expect(screen.getByRole("button", { name: "Open in a terminal" })).toBeTruthy();
  claude.unmount();
  render(<LocalSessionPanel {...props} session={{ ...app, provider: "cursor", terminalReason: "unknown" }} />);
  expect(screen.queryByRole("button", { name: "Open in a terminal" })).toBeNull();
  expect(screen.getByText(/runs in the person's own terminal/)).toBeTruthy();
});

describe("one body: the live terminal, or the conversation, never both (owner ruling 2026-10-27)", () => {
  const events = [
    { seq: 1, truncated: false, kind: "text", role: "user", text: "fix it" },
    { seq: 2, truncated: false, kind: "text", role: "assistant", text: "Done." }];
  const props = { ownerName: "Alice", own: true, client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  it("shows only the terminal while it is live: no Conversation tab and no transcript under it", async () => {
    client.listLocalSessionEvents.mockResolvedValue({ events, nextAfterSeq: null } as never);
    render(<LocalSessionPanel {...props} session={{ ...session, latestEventSeq: 2 }} />);
    await screen.findByRole("region", { name: "Live terminal (view only)" });
    await new Promise(r => setTimeout(r, 50));
    expect(screen.queryByRole("tab", { name: "Conversation" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Native session history" })).toBeNull();
    expect(screen.queryByText("Done.")).toBeNull();
    client.listLocalSessionEvents.mockResolvedValue({ events: [], nextAfterSeq: null } as never);
  });

  it("draws the conversation once when sharing is paused, says Paused at once, and shows no frozen picture", async () => {
    client.listLocalSessionEvents.mockResolvedValue({ events, nextAfterSeq: null } as never);
    render(<LocalSessionPanel {...props} session={{ ...session, latestEventSeq: 2, shared: false }} />);
    expect(await screen.findAllByText("Done.")).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Live terminal (view only)" })).toBeNull();
    expect(document.querySelector(".session-status-line")!.textContent).toMatch(/^Paused/);
    client.listLocalSessionEvents.mockResolvedValue({ events: [], nextAfterSeq: null } as never);
  });

  it("opens a search hit on its turn, then goes back to the live terminal", async () => {
    client.listLocalSessionEvents.mockResolvedValue({ events, nextAfterSeq: null } as never);
    render(<LocalSessionPanel {...props} session={{ ...session, latestEventSeq: 2 }} focusSeq={2} />);
    expect(await screen.findAllByText("Done.")).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Live terminal (view only)" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Back to the live terminal" }));
    expect(await screen.findByRole("region", { name: "Live terminal (view only)" })).toBeTruthy();
    expect(screen.queryByText("Done.")).toBeNull();
    client.listLocalSessionEvents.mockResolvedValue({ events: [], nextAfterSeq: null } as never);
  });
});

describe("a question answered from Lightbulb", () => {
  const asking = (questions: unknown) => ({ events: [
    { seq: 1, truncated: false, kind: "tool", role: "assistant", text: "q", itemId: "q1", tool: { name: "AskUserQuestion", status: "running", input: { questions } } },
    { seq: 2, truncated: false, kind: "lifecycle", role: "system", text: "q", lifecycle: "approval_pending" }], nextAfterSeq: null });
  const props = { ownerName: "Alice", own: true, client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const waiting = { ...session, status: "waiting-approval" as const, latestEventSeq: 2 };
  const tea = { question: "Tea or coffee?", options: [{ label: "Tea" }, { label: "Coffee" }] };

  it("types one option's digit for a single question, and no longer says to answer in the terminal", async () => {
    typing.typed = [];
    client.listLocalSessionEvents.mockResolvedValueOnce(asking([tea]) as never);
    render(<LocalSessionPanel {...props} session={waiting} />);
    fireEvent.click(await screen.findByRole("button", { name: "Coffee" }));
    await waitFor(() => expect(typing.typed).toEqual(["2"]));
    expect(screen.queryByText("Answer it in the live terminal below.")).toBeNull();
    expect(await screen.findByText("Sent to the terminal.")).toBeTruthy();
  });

  it("says it could not send, and keeps the buttons, when the relay fails a key", async () => {
    typing.typed = []; typing.calls = 0; typing.failOn = [0];
    client.listLocalSessionEvents.mockResolvedValueOnce(asking([tea]) as never);
    render(<LocalSessionPanel {...props} session={waiting} />);
    fireEvent.click(await screen.findByRole("button", { name: "Coffee" }));
    expect(await screen.findByText("Couldn't send, try again.")).toBeTruthy();
    expect(screen.queryByText("Sent to the terminal.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Coffee" }));
    expect(await screen.findByText("Sent to the terminal.")).toBeTruthy();
    expect(typing.typed).toEqual(["2"]);
  });

  it("after a partial send, tries again from the key that failed, not from the first", async () => {
    typing.typed = []; typing.calls = 0; typing.failOn = [1];
    client.listLocalSessionEvents.mockResolvedValueOnce(asking([tea, { question: "Hot or cold?", options: [{ label: "Hot" }, { label: "Cold" }] }]) as never);
    render(<LocalSessionPanel {...props} session={waiting} />);
    fireEvent.click(await screen.findByRole("button", { name: "Coffee" }));
    fireEvent.click(screen.getByRole("button", { name: "Hot" }));
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
    expect(await screen.findByText("Couldn't send, try again.", undefined, { timeout: 3000 })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Coffee" }).matches(":disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Sent to the terminal.", undefined, { timeout: 3000 })).toBeTruthy();
    expect(typing.typed).toEqual(["2", "1", "\r"]);
    typing.failOn = [];
  });

  it("offers no retry when a key may have landed (lost connection), even a single-select digit", async () => {
    typing.typed = []; typing.calls = 0; typing.failOn = [0]; typing.failWith = null;
    client.listLocalSessionEvents.mockResolvedValueOnce(asking([tea]) as never);
    render(<LocalSessionPanel {...props} session={waiting} />);
    fireEvent.click(await screen.findByRole("button", { name: "Coffee" }));
    expect(await screen.findByText("Couldn't send. Finish answering in the terminal.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Coffee" }).matches(":disabled")).toBe(true);
    typing.failOn = []; typing.failWith = "RATE_LIMITED";
  });

  it("offers no retry on LOCAL_SESSION_OFFLINE: a sent frame whose ack was lost reads the same", async () => {
    typing.typed = []; typing.calls = 0; typing.failOn = [0]; typing.failWith = "LOCAL_SESSION_OFFLINE";
    client.listLocalSessionEvents.mockResolvedValueOnce(asking([tea]) as never);
    render(<LocalSessionPanel {...props} session={waiting} />);
    fireEvent.click(await screen.findByRole("button", { name: "Coffee" }));
    expect(await screen.findByText("Couldn't send. Finish answering in the terminal.")).toBeTruthy();
    typing.failOn = []; typing.failWith = "RATE_LIMITED";
  });

  it("offers no retry when a multi-select toggle failed with an unknown answer", async () => {
    typing.typed = []; typing.calls = 0; typing.failOn = [1]; typing.failWith = "LOCAL_SESSION_CONFLICT";
    client.listLocalSessionEvents.mockResolvedValueOnce(asking([{ question: "Add what?", multiSelect: true, options: [{ label: "Milk" }, { label: "Sugar" }, { label: "Honey" }] }]) as never);
    render(<LocalSessionPanel {...props} session={waiting} />);
    fireEvent.click(await screen.findByRole("button", { name: "Milk" }));
    fireEvent.click(screen.getByRole("button", { name: "Honey" }));
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
    expect(await screen.findByText("Couldn't send. Finish answering in the terminal.", undefined, { timeout: 3000 })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Send answer" }).matches(":disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "Milk" }).matches(":disabled")).toBe(true);
    expect(typing.typed).toEqual(["1"]);
    typing.failOn = []; typing.failWith = "RATE_LIMITED";
  });

  it("types each pick, then Enter for the review screen, for two questions", async () => {
    typing.typed = [];
    client.listLocalSessionEvents.mockResolvedValueOnce(asking([tea, { question: "Hot or cold?", options: [{ label: "Hot" }, { label: "Cold" }] }]) as never);
    render(<LocalSessionPanel {...props} session={waiting} />);
    fireEvent.click(await screen.findByRole("button", { name: "Coffee" }));
    fireEvent.click(screen.getByRole("button", { name: "Hot" }));
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
    await waitFor(() => expect(typing.typed).toEqual(["2", "1", "\r"]), { timeout: 3000 });
  });

  it("toggles a multi-select question, moves on with Right, then Enter", async () => {
    typing.typed = [];
    client.listLocalSessionEvents.mockResolvedValueOnce(asking([{ question: "Add what?", multiSelect: true, options: [{ label: "Milk" }, { label: "Sugar" }, { label: "Honey" }] }]) as never);
    render(<LocalSessionPanel {...props} session={waiting} />);
    fireEvent.click(await screen.findByRole("button", { name: "Milk" }));
    fireEvent.click(screen.getByRole("button", { name: "Honey" }));
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
    await waitFor(() => expect(typing.typed).toEqual(["1", "3", "\x1b[C", "\r"]), { timeout: 3000 });
  });

  it("keeps the terminal line when the viewer cannot type", async () => {
    typing.canType = false;
    client.listLocalSessionEvents.mockResolvedValueOnce(asking([tea]) as never);
    render(<LocalSessionPanel {...props} session={waiting} />);
    expect(await screen.findByText(/this prompt can't be answered from here/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Coffee" })).toBeNull();
    typing.canType = true;
  });
});
