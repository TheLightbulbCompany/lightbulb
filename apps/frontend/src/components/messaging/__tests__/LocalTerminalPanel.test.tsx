import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { LocalSessionPanel } from "../LocalSessionPanel";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", orgId: "org_1" }) }));

const session: LocalSession = { sessionId: "session", deviceId: "mac", humanUserId: "alice", provider: "claude", terminal: "held", generation: "g1", status: "idle", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };
const client = () => ({ listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), getLocalSession: vi.fn(async (): Promise<LocalSession> => ({ ...session, status: "ended" })), listWorkspaceSessions: vi.fn(), setLocalSessionSharing: vi.fn() });
const panel = (s: LocalSession) => <LocalSessionPanel session={s} ownerName="Alice" own client={client() as never} revision={0} onAccessLost={() => {}} />;
// The spy sees the terminal's own calls; the panel's audience read (`local_session_audience`) goes past it.
const bridge = (invoke: (command: string, args?: unknown) => Promise<unknown>) => {
  (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke: (command: string, args?: unknown) => command === "local_session_audience" ? Promise.resolve(undefined) : invoke(command, args) } };
};
const desktop = (reply: unknown) => {
  const invoke = vi.fn(async () => reply);
  bridge(invoke);
  return invoke;
};
beforeEach(() => { vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 300, top: 120, width: 800, height: 500 } as DOMRect); });
afterEach(() => { delete (window as { __TAURI__?: unknown }).__TAURI__; vi.restoreAllMocks(); });

it("a session this Mac runs shows its live terminal in place of the transcript, and unmounting only hides it", async () => {
  const invoke = desktop({ live: true });
  const { unmount } = render(panel(session));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("terminal_panel", { sessionId: "session", rect: { x: 300, y: 120, width: 800, height: 500 }, viewport: { width: innerWidth, height: innerHeight }, at: expect.any(Number) }));
  expect(screen.getByRole("region", { name: "Session terminal" })).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /send to/i })).toBeNull();
  expect(screen.queryByRole("region", { name: "Native session history" })).toBeNull();
  unmount();
  expect(invoke).toHaveBeenLastCalledWith("terminal_panel", { sessionId: "session", rect: null, at: expect.any(Number) });
});

it("a session this Mac does not run keeps the session view", async () => {
  desktop({ live: false });
  render(panel(session));
  expect(await screen.findByRole("textbox", { name: /send to/i })).toBeInTheDocument();
});

it("Details takes the terminal away so the sheet is not drawn under it", async () => {
  const invoke = desktop({ live: true });
  render(panel(session));
  await waitFor(() => expect(invoke).toHaveBeenCalled());
  await userEvent.click(screen.getByRole("button", { name: "Details" }));
  await screen.findByRole("dialog");
  await waitFor(() => expect(invoke).toHaveBeenLastCalledWith("terminal_panel", expect.objectContaining({ rect: null })));
});

// Round 18: "End and remove this session?" opened under the terminal, which the app draws above the whole page.
it("a modal dialog takes the terminal away and closing it brings it back; a popover or the emoji picker does not", async () => {
  const invoke = desktop({ live: true });
  render(<>{panel(session)}<Dialog open><DialogContent><DialogTitle>End and remove “x”?</DialogTitle><DialogDescription>It stops.</DialogDescription></DialogContent></Dialog></>);
  // The app's own confirm dialog (what SessionRemoveButton renders), not a stand-in.
  await waitFor(() => expect(invoke).toHaveBeenLastCalledWith("terminal_panel", expect.objectContaining({ rect: null })));
  cleanup();
  invoke.mockClear();
  render(panel(session));
  await waitFor(() => expect(invoke).toHaveBeenLastCalledWith("terminal_panel", expect.objectContaining({ rect: expect.anything() })));
  // Not modal (Radix Popover content and the emoji picker are `role="dialog"` too): the terminal stays.
  const popover = document.body.appendChild(Object.assign(document.createElement("div"), { role: "dialog" }));
  await new Promise((r) => setTimeout(r, 300));
  expect(invoke).toHaveBeenLastCalledWith("terminal_panel", expect.objectContaining({ rect: expect.anything() }));
  popover.remove();
  const dialog = document.body.appendChild(Object.assign(document.createElement("div"), { role: "presentation" }));
  dialog.setAttribute("data-modal-overlay", "");
  await waitFor(() => expect(invoke).toHaveBeenLastCalledWith("terminal_panel", expect.objectContaining({ rect: null })));
  dialog.remove();
  await waitFor(() => expect(invoke).toHaveBeenLastCalledWith("terminal_panel", expect.objectContaining({ rect: expect.anything() })));
});

it("an ended session never asks for a terminal", async () => {
  const invoke = desktop({ live: true });
  render(panel({ ...session, status: "ended" }));
  expect(await screen.findByText("This session ended before anything was sent.")).toBeInTheDocument();
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
    // A base implementation (Tauri's invoke always returns a promise): restoreAllMocks keeps it,
    // so the unmount's rect-null call still gets a promise.
    let calls = 0;
    const invoke = vi.fn(async () => ({ live: calls++ > 0 }));
    bridge(invoke);
    render(panel(session));
    await vi.advanceTimersByTimeAsync(100);
    expect(screen.getByRole("textbox", { name: /send to/i })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Session terminal" })).toBeNull();
    await vi.advanceTimersByTimeAsync(2500);
    // The slot was gone while the answer was "none": the probe asks without a rect.
    expect(invoke).toHaveBeenCalledWith("terminal_panel", { sessionId: "session", rect: null, at: expect.any(Number) });
    expect(screen.getByRole("region", { name: "Session terminal" })).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(300);
    expect(invoke).toHaveBeenLastCalledWith("terminal_panel", { sessionId: "session", rect: { x: 300, y: 120, width: 800, height: 500 }, viewport: { width: innerWidth, height: innerHeight }, at: expect.any(Number) });
  } finally { vi.useRealTimers(); }
});

it("keeps asking after the first half minute, more slowly, while the session runs", async () => {
  vi.useFakeTimers();
  try {
    let live = false;
    const invoke = vi.fn(async () => ({ live }));
    bridge(invoke);
    render(panel(session));
    await vi.advanceTimersByTimeAsync(31_000);
    const early = invoke.mock.calls.length;
    live = true;
    await vi.advanceTimersByTimeAsync(10_500);
    expect(invoke.mock.calls.length).toBeGreaterThan(early);
    expect(screen.getByRole("region", { name: "Session terminal" })).toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});
