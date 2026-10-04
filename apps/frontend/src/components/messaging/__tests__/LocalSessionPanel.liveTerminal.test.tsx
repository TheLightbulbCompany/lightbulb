import { act, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";
import { sessionStatus, type LocalSession } from "@lightbulb/shared";
import { LocalSessionPanel } from "../LocalSessionPanel";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", orgId: "org_1" }) }));
// The terminal itself is tested in SessionTerminalView.test; here it just reports that it is live.
const typing = vi.hoisted(() => ({ canType: true }));
vi.mock("../SessionTerminalView", async () => {
  const { useEffect } = await import("react");
  return { SessionTerminalView: ({ onLive, onCanType }: { onLive: (live: boolean) => void; onCanType?: (c: boolean) => void }) => {
    useEffect(() => { onLive(true); onCanType?.(typing.canType); }, [onLive, onCanType]);
    return <div role="region" aria-label="Live terminal (view only)" />;
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

it("labels a session that is not running as a transcript, and a live one not at all", () => {
  const props = { ownerName: "Alice", client: client as never, revision: 0, onAccessLost: () => {} };
  const { unmount } = render(<LocalSessionPanel {...props} own={false} session={{ ...session, status: "ended" }} />);
  expect(screen.getByText("Transcript · Ended")).toBeTruthy();
  unmount();
  const offline = render(<LocalSessionPanel {...props} own={false} session={{ ...session, status: "offline" }} />);
  expect(screen.getByText("Transcript · Alice's Mac is offline")).toBeTruthy();
  expect(document.querySelector(".session-status-line")!.textContent!.match(/Alice/g)).toHaveLength(1);
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

it("shows a plain Claude session's transcript with its status, and tells its owner how to get the terminal", () => {
  client.watchLocalSession.mockClear();
  const plain: LocalSession = { ...session, terminal: undefined, status: "busy" };
  render(<LocalSessionPanel session={plain} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  expect(screen.getByText(`Transcript · ${sessionStatus.busy}`)).toBeTruthy();
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

it("gives a plain Codex session and a teammate no ← hint", () => {
  const props = { ownerName: "Alice", client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const codex = render(<LocalSessionPanel {...props} own session={{ ...session, provider: "codex", terminal: undefined }} />);
  expect(screen.queryByText(/Press ←/)).toBeNull();
  codex.unmount();
  render(<LocalSessionPanel {...props} own={false} session={{ ...session, terminal: undefined }} />);
  expect(screen.queryByText(/Press ←/)).toBeNull();
  expect(screen.getByText(`Transcript · ${sessionStatus.idle}`)).toBeTruthy();
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
  render(<LocalSessionPanel {...props} session={{ ...session, status: "ended", latestEventSeq: 4, resumable: true }} />);
  expect(await screen.findByRole("button", { name: "Resume" })).toBeTruthy();
});
