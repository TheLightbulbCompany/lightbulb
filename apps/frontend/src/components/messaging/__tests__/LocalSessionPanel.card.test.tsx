import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { LocalSessionPanel } from "../LocalSessionPanel";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", orgId: "org_1" }) }));
vi.mock("../SessionControls", () => ({ SessionApprovalCard: () => <section aria-label="Permission prompt" /> }));
vi.mock("../SessionTerminalView", async () => {
  const { useEffect } = await import("react");
  return { SessionTerminalView: ({ onLive }: { onLive: (live: boolean) => void }) => {
    useEffect(() => { onLive(true); }, [onLive]);
    return <div role="region" aria-label="Live terminal" />;
  } };
});

const session: LocalSession = { sessionId: "s", deviceId: "mac", humanUserId: "alice", provider: "claude", terminal: "held", generation: "g1", status: "waiting-approval", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };
const client = { listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(async () => ({ requestId: "r", state: "received" })), getLocalSession: vi.fn(), listWorkspaceSessions: vi.fn(), setLocalSessionSharing: vi.fn(), watchLocalSession: vi.fn(async () => undefined), sendLocalSessionInput: vi.fn(), listLocalSessionOutbox: vi.fn(async () => ({ commands: [] })) };
const props = { ownerName: "Alice", own: true, client: client as never, revision: 0, onAccessLost: () => {} };

it("hides the approval card while a live terminal is on screen: its own prompt is the surface (ruling 2026-10-27)", async () => {
  render(<LocalSessionPanel {...props} session={session} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal" });
  expect(screen.queryByRole("region", { name: "Permission prompt" })).toBeNull();
});

it("shows the approval card where no terminal is open", () => {
  render(<LocalSessionPanel {...props} session={session} />);
  expect(screen.getByRole("region", { name: "Permission prompt" })).toBeTruthy();
});

it("drops the End chip once the session has ended", async () => {
  const view = render(<LocalSessionPanel {...props} session={{ ...session, status: "idle" }} onEvent={() => () => {}} />);
  const end = screen.getByRole("button", { name: "End session" });
  fireEvent.click(end);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Click again to end/ })); });
  const chip = () => document.querySelector(".session-status-actions [role=status]");
  expect(chip()).not.toBeNull();
  view.rerender(<LocalSessionPanel {...props} session={{ ...session, status: "ended", endedAt: new Date().toISOString() }} onEvent={() => () => {}} />);
  expect(chip()).toBeNull();
});
