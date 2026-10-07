import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";
import { type LocalSession } from "@lightbulb/shared";
import { LocalSessionPanel } from "../LocalSessionPanel";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", orgId: "org_1" }) }));
// The terminal itself is tested in SessionTerminalView.test; here it just reports that it is live.
const typing = vi.hoisted(() => ({ canType: true, admitted: undefined as boolean | undefined }));
vi.mock("../SessionTerminalView", async () => {
  const { useEffect } = await import("react");
  return { SessionTerminalView: ({ onLive, onCanType, admitted }: { onLive: (live: boolean) => void; onCanType?: (c: boolean) => void; admitted?: boolean }) => {
    typing.admitted = admitted;
    useEffect(() => { onLive(true); onCanType?.(typing.canType); }, [onLive, onCanType]);
    return <div role="region" aria-label="Live terminal (view only)"><div className="session-terminal-scroll" /></div>;
  } };
});

const session: LocalSession = { sessionId: "s", deviceId: "mac", humanUserId: "alice", provider: "claude", terminal: "held", generation: "g1", status: "idle", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };
const client = { listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSession: vi.fn(), listWorkspaceSessions: vi.fn(), setLocalSessionSharing: vi.fn(), watchLocalSession: vi.fn(async () => undefined), sendLocalSessionInput: vi.fn() };
const below = (a: HTMLElement, b: HTMLElement) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

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

it("shows Watching · Ask to join, and no text box, under a live terminal for a teammate who may not send", async () => {
  render(<LocalSessionPanel session={session} ownerName="Alice" own={false} client={{ ...client, requestLocalSessionJoin: vi.fn() } as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  const terminal = await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(below(terminal, await screen.findByText(/Watching · Only Alice can send/))).toBe(true);
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByRole("button", { name: "Ask to join" })).toBeTruthy();
});

it("shows who is typing into the owner's session for 6 s after the last event", async () => {
  vi.useFakeTimers();
  let emit: (e: LiveEvent) => void = () => {};
  render(<LocalSessionPanel session={session} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} nameOf={(id) => (id === "bob" ? "Bob" : "?")}
    onEvent={(h) => { emit = h; return () => {}; }} />);
  act(() => { emit({ schemaVersion: 1, eventId: "t1", type: "workspace.session.typing", occurredAt: "now", payload: { sessionId: "s", userId: "bob" } } as LiveEvent); });
  expect(screen.getByText("Bob is typing")).toBeTruthy();
  await act(async () => { await vi.advanceTimersByTimeAsync(6_001); });
  expect(screen.queryByText("Bob is typing")).toBeNull();
  vi.useRealTimers();
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

it("tells a plain session's owner it was not started through Lightbulb and how to get the terminal", () => {
  client.watchLocalSession.mockClear();
  const plain: LocalSession = { ...session, terminal: undefined, status: "busy" };
  render(<LocalSessionPanel session={plain} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  expect(screen.getByText("This session was not started through Lightbulb, so its screen is not here.")).toBeTruthy();
  const hint = "Press ← in its terminal to share this one live, or turn on Share every session live in Settings to make every new session live.";
  expect(screen.getByText((_, el) => el?.tagName === "P" && el.textContent === hint)).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
  expect(screen.queryByRole("region", { name: /Live terminal/ })).toBeNull();
  expect(client.watchLocalSession).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox", { name: /send to/i })).toBeTruthy();
});

it("links the hint's Settings to the live-sessions switch in the desktop app", () => {
  (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke: vi.fn(async () => ({ live: false })) } };
  try {
    render(<LocalSessionPanel session={{ ...session, terminal: undefined, status: "busy" }} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
    expect(screen.getByRole("link", { name: "Settings" }).getAttribute("href")).toBe("/settings?panel=desktop#share-every-session-live");
  } finally {
    delete (window as { __TAURI__?: unknown }).__TAURI__;
  }
});

it("gives a plain Codex session no hint, and a teammate of a plain Claude session the Ask line", () => {
  const props = { ownerName: "Alice", client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const codex = render(<LocalSessionPanel {...props} own={false} session={{ ...session, provider: "codex", terminal: undefined }} />);
  expect(screen.getByText("This session was not started through Lightbulb, so its screen is not here.")).toBeTruthy();
  expect(screen.queryByText(/press ←/i)).toBeNull();
  expect(screen.queryByText(/Ask Alice/)).toBeNull();
  codex.unmount();
  render(<LocalSessionPanel {...props} own={false} session={{ ...session, terminal: undefined }} />);
  expect(screen.queryByText(/Press ←/)).toBeNull();
  expect(screen.getByText("Ask Alice to press ← in their terminal.")).toBeTruthy();
  expect(document.querySelector(".session-status-line")!.textContent).not.toMatch(/Transcript/);
});

it("still probes this Mac's holder for an offline session, and never for a running one with no terminal", () => {
  const invoke = vi.fn(async () => ({ live: false }));
  (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
  try {
    const props = { ownerName: "Alice", own: true, client: client as never, revision: 0, onAccessLost: () => {} };
    const offline = render(<LocalSessionPanel {...props} session={{ ...session, status: "offline" }} />);
    expect(invoke).toHaveBeenCalledWith("terminal_panel", expect.objectContaining({ sessionId: "s" }));
    offline.unmount();
    invoke.mockClear();
    render(<LocalSessionPanel {...props} session={{ ...session, terminal: undefined, status: "busy" }} />);
    expect(invoke).not.toHaveBeenCalled();
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
  expect(await screen.findByRole("button", { name: "Resume" })).toBeTruthy();
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

const textEvent = (seq: number) => ({ seq, kind: "text", role: "user", text: `t${seq}` });
const change = { seq: 3, kind: "tool", role: "assistant", itemId: "c1", tool: { name: "FileChange", status: "completed", result: { "a.ts": { type: "add", content: "x\n" } } } };

it("keeps loading older pages without a scroll so the plan and changed files see the whole session", async () => {
  const list = vi.fn(async (_id: string, after: number) => ({ events: after === 0 ? [textEvent(1), textEvent(2), change, ...Array.from({ length: 7 }, (_, i) => textEvent(4 + i))] : Array.from({ length: 50 }, (_, i) => textEvent(11 + i)), nextAfterSeq: null }));
  render(<LocalSessionPanel session={{ ...session, status: "idle", latestEventSeq: 60 }} ownerName="Alice" own client={{ ...client, listLocalSessionEvents: list } as never} revision={0} onAccessLost={() => {}} />);
  await waitFor(() => expect(list).toHaveBeenCalledWith("s", 0, 10));
  expect(await screen.findByText(/1 file changed/)).toBeTruthy();
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
  expect(await screen.findByRole("button", { name: "Resume" })).toBeTruthy();
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

it("tells how a no-daemon Codex session gets its screen, and says nothing for a reason it cannot act on", () => {
  const props = { ownerName: "Alice", client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const codex: LocalSession = { ...session, provider: "codex", terminal: undefined, terminalReason: "no-daemon" };
  const line = (text: string) => screen.getByText((_, el) => el?.tagName === "P" && el.textContent === text);
  const mine = render(<LocalSessionPanel {...props} own session={codex} />);
  expect(line("Start it again with lightbulb codex to share its screen.")).toBeTruthy();
  mine.unmount();
  const theirs = render(<LocalSessionPanel {...props} own={false} session={codex} />);
  expect(line("Ask Alice to start it again with lightbulb codex.")).toBeTruthy();
  expect(screen.queryByText(/press ←/i)).toBeNull();
  theirs.unmount();
  render(<LocalSessionPanel {...props} own={false} session={{ ...session, terminal: undefined, terminalReason: "unknown" }} />);
  expect(screen.getByText("This session was not started through Lightbulb, so its screen is not here.")).toBeTruthy();
  expect(screen.queryByText(/press ←/i)).toBeNull();
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
