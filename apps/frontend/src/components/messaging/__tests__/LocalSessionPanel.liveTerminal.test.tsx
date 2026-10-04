import { act, render, screen } from "@testing-library/react";
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
  expect(screen.getByText("Press ← in its terminal to open it here.")).toBeTruthy();
  expect(screen.queryByRole("region", { name: /Live terminal/ })).toBeNull();
  expect(client.watchLocalSession).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox", { name: /send to/i })).toBeTruthy();
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
