import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { LocalSessionPanel } from "../LocalSessionPanel";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", orgId: "org_1" }) }));

const session: LocalSession = { sessionId: "session", deviceId: "mac", humanUserId: "alice", provider: "claude", generation: "g1", status: "idle", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };
const client = () => ({ listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), getLocalSession: vi.fn(), listWorkspaceSessions: vi.fn(), setLocalSessionSharing: vi.fn() });
const panel = (s: LocalSession) => <LocalSessionPanel session={s} ownerName="Alice" own client={client() as never} revision={0} onAccessLost={() => {}} />;
const desktop = (reply: unknown) => {
  const invoke = vi.fn(async () => reply);
  (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
  return invoke;
};
beforeEach(() => { vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 300, top: 120, width: 800, height: 500 } as DOMRect); });
afterEach(() => { delete (window as { __TAURI__?: unknown }).__TAURI__; vi.restoreAllMocks(); });

it("a session this Mac runs shows its live terminal in place of the transcript, and unmounting only hides it", async () => {
  const invoke = desktop({ live: true });
  const { unmount } = render(panel(session));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("terminal_panel", { sessionId: "session", rect: { x: 300, y: 120, width: 800, height: 500 }, at: expect.any(Number) }));
  expect(screen.getByRole("region", { name: "Session terminal" })).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Native session history" })).toBeNull();
  unmount();
  expect(invoke).toHaveBeenLastCalledWith("terminal_panel", { sessionId: "session", rect: null, at: expect.any(Number) });
});

it("a session this Mac does not run keeps the session view", async () => {
  desktop({ live: false });
  render(panel(session));
  expect(await screen.findByRole("region", { name: "Native session history" })).toBeInTheDocument();
});

it("Details takes the terminal away so the sheet is not drawn under it", async () => {
  const invoke = desktop({ live: true });
  render(panel(session));
  await waitFor(() => expect(invoke).toHaveBeenCalled());
  await userEvent.click(screen.getByRole("button", { name: "Details" }));
  await screen.findByRole("dialog");
  await waitFor(() => expect(invoke).toHaveBeenLastCalledWith("terminal_panel", expect.objectContaining({ rect: null })));
});

it("an ended session never asks for a terminal", async () => {
  const invoke = desktop({ live: true });
  render(panel({ ...session, status: "ended" }));
  expect(await screen.findByRole("region", { name: "Native session history" })).toBeInTheDocument();
  expect(invoke).not.toHaveBeenCalledWith("terminal_panel", expect.anything());
});

it("a close and the next open in one tick carry strictly increasing times", async () => {
  vi.spyOn(performance, "now").mockReturnValue(5);
  const invoke = desktop({ live: true });
  const first = render(panel(session));
  await waitFor(() => expect(invoke).toHaveBeenCalled());
  first.unmount();
  render(panel(session));
  const ats = invoke.mock.calls.map(c => (c as unknown as [string, { at: number }])[1].at);
  expect(ats.length).toBeGreaterThanOrEqual(3);
  ats.forEach((at, i) => { if (i) expect(at).toBeGreaterThan(ats[i - 1]); });
});

it("keeps asking while no holder answers, and promotes the panel when one does", async () => {
  vi.useFakeTimers();
  try {
    const invoke = vi.fn().mockResolvedValueOnce({ live: false }).mockResolvedValue({ live: true });
    (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
    render(panel(session));
    await vi.advanceTimersByTimeAsync(100);
    expect(screen.getByRole("region", { name: "Native session history" })).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(2500);
    expect(screen.getByRole("region", { name: "Session terminal" })).toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});
