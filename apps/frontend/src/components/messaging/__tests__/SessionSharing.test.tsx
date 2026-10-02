import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { LocalSessionPanel } from "../LocalSessionPanel";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isSignedIn: true, isLoaded: true, userId: "user_test_123", orgId: "org_1", getToken: async () => "jwt" }),
}));
import { capture } from "@/lib/analytics";

const session: LocalSession = { sessionId: "session", deviceId: "mac", humanUserId: "alice", provider: "claude", generation: "g1", status: "idle", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };
const client = (setLocalSessionSharing = vi.fn(async (_: string, shared: boolean) => ({ ...session, shared }))) => ({
  listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(),
  getLocalSession: vi.fn(), listWorkspaceSessions: vi.fn(), setLocalSessionSharing,
});
const panel = (s: LocalSession, own: boolean, c: ReturnType<typeof client>) =>
  <LocalSessionPanel session={s} ownerName="Alice" own={own} client={c as never} revision={0} onAccessLost={() => {}} />;

async function access() {
  await userEvent.click(screen.getByRole("button", { name: "Details" }));
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("tab", { name: "Access" }));
  return dialog;
}

describe("per-session sharing", () => {
  it("lets the owner stop sharing one session, then resume it", async () => {
    const c = client();
    const { rerender } = render(panel(session, true, c));
    expect(screen.queryByText(/^Sharing is paused/)).not.toBeInTheDocument();
    const dialog = await access();
    await userEvent.click(within(dialog).getByRole("switch", { name: "Shared with the workspace" }));
    expect(c.setLocalSessionSharing).toHaveBeenCalledWith("session", false);
    expect(within(dialog).getByRole("switch", { name: "Shared with the workspace" })).not.toBeChecked();
    // The session row comes back paused: the pane itself says so.
    rerender(panel({ ...session, shared: false }, true, c));
    expect(screen.getByText("Sharing is paused. Only you can see this session, and nothing it does now is shared.")).toHaveAttribute("role", "status");
    await userEvent.click(within(dialog).getByRole("switch", { name: "Shared with the workspace" }));
    expect(c.setLocalSessionSharing).toHaveBeenLastCalledWith("session", true);
  });

  it("never offers it to a teammate", async () => {
    render(panel(session, false, client()));
    const dialog = await access();
    expect(within(dialog).queryByRole("switch", { name: "Shared with the workspace" })).toBeNull();
    expect(within(dialog).getByText(/^Only Alice can send to this session/)).toBeInTheDocument();
  });
});

describe("session analytics", () => {
  it("reports session_opened once, with whether the session is your own", () => {
    render(panel(session, false, client()));
    const opened = vi.mocked(capture).mock.calls.filter((call) => call[0] === "session_opened");
    expect(opened).toEqual([["session_opened", { own: false, provider: "claude", workspace_id: "org_1", session_id: session.sessionId, origin: "web" }]]);
  });
});
