import { act, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { LiveEvent } from "@lightbulb/shared";
import type { LocalSession } from "@lightbulb/shared";
import { LocalSessionPanel } from "../LocalSessionPanel";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", orgId: "org_1" }) }));
// The terminal itself is tested in SessionTerminalView.test; here it just reports that it is live.
vi.mock("../SessionTerminalView", async () => {
  const { useEffect } = await import("react");
  return { SessionTerminalView: ({ onLive }: { onLive: (live: boolean) => void }) => {
    useEffect(() => { onLive(true); }, [onLive]);
    return <div role="region" aria-label="Live terminal (view only)" />;
  } };
});

const session: LocalSession = { sessionId: "s", deviceId: "mac", humanUserId: "alice", provider: "claude", generation: "g1", status: "idle", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };
const client = { listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSession: vi.fn(), listWorkspaceSessions: vi.fn(), setLocalSessionSharing: vi.fn(), watchLocalSession: vi.fn(async () => undefined), sendLocalSessionInput: vi.fn() };
const noInput = { ...client, sendLocalSessionInput: undefined };
const below = (a: HTMLElement, b: HTMLElement) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

it("drops the message box and the history region when the viewer can type in the live terminal", async () => {
  render(<LocalSessionPanel session={session} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(screen.queryByRole("region", { name: "Native session history" })).toBeNull();
  expect(screen.queryByRole("textbox", { name: /send to/i })).toBeNull();
});

it("shows no text box under a live terminal even when sending is unavailable", async () => {
  render(<LocalSessionPanel session={session} ownerName="Alice" own client={noInput as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(screen.queryByRole("textbox")).toBeNull();
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
  offline.unmount();
  render(<LocalSessionPanel {...props} own={false} session={session} />);
  expect(screen.queryByText(/Transcript/)).toBeNull();
});
