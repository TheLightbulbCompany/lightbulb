import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
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
const client = { listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSession: vi.fn(), listWorkspaceSessions: vi.fn(), setLocalSessionSharing: vi.fn(), watchLocalSession: vi.fn(async () => undefined) };
const below = (a: HTMLElement, b: HTMLElement) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

it("keeps the message box under the live terminal, and drops the history region", async () => {
  render(<LocalSessionPanel session={session} ownerName="Alice" own client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  const terminal = await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(screen.queryByRole("region", { name: "Native session history" })).toBeNull();
  expect(below(terminal, screen.getByRole("textbox", { name: /send to/i }))).toBe(true);
});

it("keeps Ask to join under the live terminal for a teammate who may not send", async () => {
  render(<LocalSessionPanel session={session} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  const terminal = await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(below(terminal, await screen.findByText(/Only Alice can send/))).toBe(true);
});
