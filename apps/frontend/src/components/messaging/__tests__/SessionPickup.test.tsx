import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ApiError, type LocalSession, type LocalSessionPickup, type LocalSessionPickupTicket } from "@lightbulb/shared";
import { SessionPickup } from "../SessionPickup";

const ready: LocalSessionPickup = { allowed: true, state: "ready", hasCode: true, repo: "github.com/acme/app", branch: "fix/login", snapshotAt: new Date().toISOString() };
const session = (pickup: LocalSessionPickup | undefined = ready): LocalSession => ({
  sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.283", generation: "g", status: "idle",
  activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null,
  latestEventSeq: 3, title: "Fix login", pickup,
});
const ticket = { pickupId: "p", expiresAt: "2026-09-27T12:00:00Z" } as LocalSessionPickupTicket;
const client = () => ({
  requestLocalSessionPickup: vi.fn(async () => ticket),
  reportLocalSessionPickupOutcome: vi.fn(async () => {}),
});
type Invoke = ReturnType<typeof vi.fn>;
/** Runs inside the desktop app: `window.__TAURI__.core.invoke` is what the page talks to the Mac with. */
const desktop = (invoke: Invoke) => Object.assign(window, { __TAURI__: { core: { invoke } } });
afterEach(() => { delete (window as { __TAURI__?: unknown }).__TAURI__; });

async function open(s = session(), c = client()) {
  render(<SessionPickup session={s} ownerName="Alice" own={false} client={c} />);
  await userEvent.click(screen.getByRole("button", { name: "Pick up" }));
  return screen.findByRole("dialog");
}

it("offers no button unless pick-up is allowed and its snapshot is ready", () => {
  const { rerender } = render(<SessionPickup session={session({ allowed: false })} ownerName="Alice" own={false} client={client()} />);
  expect(screen.queryByRole("button", { name: "Pick up" })).not.toBeInTheDocument();
  rerender(<SessionPickup session={session({ ...ready, state: "preparing" })} ownerName="Alice" own={false} client={client()} />);
  expect(screen.queryByRole("button", { name: "Pick up" })).not.toBeInTheDocument();
  rerender(<SessionPickup session={{ ...session(), pickup: undefined }} ownerName="Alice" own={false} client={client()} />);
  expect(screen.queryByRole("button", { name: "Pick up" })).not.toBeInTheDocument();
  rerender(<SessionPickup session={session()} ownerName="Alice" own={false} client={client()} />);
  expect(screen.getByRole("button", { name: "Pick up" })).toBeInTheDocument();
});

it("in a browser, explains the desktop app is needed and offers no way to start", async () => {
  const c = client();
  const dialog = await open(session(), c);
  expect(within(dialog).getByText(/^Open Lightbulb on your Mac to pick this up\./)).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "Pick up" })).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Done" })).toBeInTheDocument();
  expect(c.requestLocalSessionPickup).not.toHaveBeenCalled();
});

it("inside the desktop app, fetches a ticket, hands it to the Mac, shows the branch and reports the outcome", async () => {
  const invoke = vi.fn(async () => ({ branch: "lightbulb/pickup-1", worktree: "/Users/bob/app-pickup", deviceId: "mac2", profileFingerprint: "fp", conversationId: "conv" }));
  desktop(invoke);
  const c = client();
  const dialog = await open(session(), c);
  expect(within(dialog).getByText("acme/app on fix/login")).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: "Pick up" }));
  expect(await within(dialog).findByText("lightbulb/pickup-1")).toBeInTheDocument();
  expect(within(dialog).getByRole("heading", { name: "Picked up" })).toBeInTheDocument();
  const [sessionId, pickupId] = c.requestLocalSessionPickup.mock.calls[0] as unknown as [string, string];
  expect(sessionId).toBe("s1");
  expect(invoke).toHaveBeenCalledWith("session_pickup", { ticket });
  await waitFor(() => expect(c.reportLocalSessionPickupOutcome).toHaveBeenCalledWith("s1", pickupId, {
    ok: true, branch: "lightbulb/pickup-1", deviceId: "mac2", profileFingerprint: "fp", conversationId: "conv",
  }));
});

it("asks which copy when the Mac has several, then re-runs the same pick-up in the chosen folder", async () => {
  const invoke = vi.fn()
    .mockRejectedValueOnce({ code: "REPO_AMBIGUOUS", candidates: ["/Users/bob/app", "/Users/bob/work/app"] })
    .mockResolvedValueOnce({ branch: "lightbulb/pickup-2" });
  desktop(invoke);
  const c = client();
  const dialog = await open(session(), c);
  await userEvent.click(within(dialog).getByRole("button", { name: "Pick up" }));
  expect(await within(dialog).findByText(/more than one copy of that repository/)).toBeInTheDocument();
  const here = within(dialog).getByRole("button", { name: "Pick up here" });
  expect(here).toBeDisabled();
  await userEvent.click(within(dialog).getByRole("radio", { name: "/Users/bob/work/app" }));
  await userEvent.click(here);
  expect(await within(dialog).findByText("lightbulb/pickup-2")).toBeInTheDocument();
  const ids = c.requestLocalSessionPickup.mock.calls.map(call => (call as unknown as [string, string])[1]);
  expect(ids).toHaveLength(2);
  expect(ids[1]).toBe(ids[0]);
  expect(invoke).toHaveBeenLastCalledWith("session_pickup", { ticket, folder: "/Users/bob/work/app" });
});

it("names the repository the Mac has no copy of", async () => {
  desktop(vi.fn().mockRejectedValue({ code: "REPO_NOT_FOUND", remote: "github.com/acme/app" }));
  const c = client();
  const dialog = await open(session(), c);
  await userEvent.click(within(dialog).getByRole("button", { name: "Pick up" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("This Mac has no copy of github.com/acme/app in an approved folder.");
  await waitFor(() => expect(c.reportLocalSessionPickupOutcome).toHaveBeenCalledWith("s1", expect.any(String), { ok: false, code: "REPO_NOT_FOUND" }));
});

it.each([403, 404, 409])("says the session can no longer be picked up when the ticket is refused with %i", async status => {
  const invoke = vi.fn();
  desktop(invoke);
  const c = { ...client(), requestLocalSessionPickup: vi.fn(async () => { throw new ApiError(status, null); }) };
  const dialog = await open(session(), c);
  await userEvent.click(within(dialog).getByRole("button", { name: "Pick up" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^This session can no longer be picked up\./);
  expect(invoke).not.toHaveBeenCalled();
});

it.each([
  ["a server error", new ApiError(503, null)],
  ["a network failure", new TypeError("Failed to fetch")],
])("says the pick-up failed and can be retried after %s", async (_, error) => {
  const invoke = vi.fn();
  desktop(invoke);
  const c = { ...client(), requestLocalSessionPickup: vi.fn(async () => { throw error; }) };
  const dialog = await open(session(), c);
  await userEvent.click(within(dialog).getByRole("button", { name: "Pick up" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(/^The pick-up could not be started\. Check your connection and try again\./);
  expect(invoke).not.toHaveBeenCalled();
});

it("asks for a folder first when the session has no repository", async () => {
  const invoke = vi.fn(async (command: string) => (command === "file_access_list_paths" ? ["/Users/bob/notes"] : { conversationOnly: true }));
  desktop(invoke);
  const c = client();
  const dialog = await open(session({ ...ready, hasCode: false, repo: null }), c);
  await userEvent.click(within(dialog).getByRole("button", { name: "Pick up" }));
  expect(await within(dialog).findByText("Which folder should the conversation continue in?")).toBeInTheDocument();
  expect(invoke).toHaveBeenCalledWith("file_access_list_paths");
  expect(c.requestLocalSessionPickup).not.toHaveBeenCalled();
  await userEvent.click(within(dialog).getByRole("radio", { name: "/Users/bob/notes" }));
  await userEvent.click(within(dialog).getByRole("button", { name: "Pick up here" }));
  expect(await within(dialog).findByText(/is open in Terminal\.$/)).toBeInTheDocument();
  expect(invoke).toHaveBeenLastCalledWith("session_pickup", { ticket, folder: "/Users/bob/notes" });
});

it("returns focus to the Pick up button when the dialog closes", async () => {
  await open();
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "Pick up" })).toHaveFocus();
});

it("says the session opened with the receiver's own Claude settings only", async () => {
  // A snapshot can carry a teammate's project settings, whose hooks run commands on start.
  desktop(vi.fn(async () => ({ ok: true, branch: "pickup/fix-login-1a2b", worktree: "/repo/.claude/worktrees/pickup-fix-login-1a2b", conversationOnly: false, settings: "user" })));
  const dialog = await open();
  await userEvent.click(within(dialog).getByRole("button", { name: "Pick up" }));
  expect(await within(dialog).findByText(/It opened with your own Claude settings only\./)).toBeInTheDocument();
  expect(within(dialog).getByText(/were not loaded/)).toBeInTheDocument();
});
