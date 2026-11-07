// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { DirectoryRoom } from "@/components/messaging/types";

const openPanel = vi.fn();
const activateMessagesPanel = vi.fn();
let activePanelId: string | null;
vi.mock("../workspaceController", () => ({
  openPanel: (...args: unknown[]) => openPanel(...args),
  activateMessagesPanel: (...args: unknown[]) => activateMessagesPanel(...args),
  useWorkspaceState: () => ({ activePanelId }),
}));
vi.mock("@/components/messaging/MessagingWorkspace", () => ({
  MessagingDirectory: ({ onOpenRoom, onNavigate }: { onOpenRoom: (room?: DirectoryRoom) => void; onNavigate?: () => void }) => (
    <nav aria-label="Chats">
      <button onClick={onNavigate}>Invite someone</button>
      {(["dm", "channel", "group-dm", "human"] as const).map((kind) => (
        <button key={kind} onClick={() => onOpenRoom({
          roomId: kind, name: null, roomRevision: 1, lastMessageSeq: 0,
          lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z",
          topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0,
          kind: kind === "human" ? "dm" : kind,
          participants: [{ kind: kind === "human" ? "human" : "agent", id: "agent-1", displayName: "Counterpart", mentionHandle: null }],
        })}>{kind}</button>
      ))}
      <button onClick={() => onOpenRoom()}>New chat or project</button>
    </nav>
  ),
}));
let billingAccount: { subscription_status: string | null; trial_end?: number | null };
let isSubscribed: boolean;
vi.mock("@/components/messaging/TrialRecapCard", () => ({ TrialRecapCard: () => null }));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ account: billingAccount, isSubscribed }) }));
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: {
      fullName: "Ada Lovelace",
      // Newest first, as Clerk lists them: the active workspace is NOT the first membership.
      organizationMemberships: [{ organization: { name: "Newer Workspace" } }, { organization: { name: "Acme" } }],
    },
  }),
  useOrganization: () => ({ organization: { id: "org_acme", name: "Acme" } }),
  useOrganizationList: () => ({ userMemberships: { data: memberships }, setActive }),
}));
let memberships: { organization: { id: string; name: string } }[] = [];
const setActive = vi.fn().mockResolvedValue(undefined);

vi.mock("@/components/chat/HealthIndicator", () => ({
  HealthIndicator: ({ onRecoveryReprovision }: { onRecoveryReprovision: () => void }) => (
    <button onClick={onRecoveryReprovision}>Recover workspace</button>
  ),
}));
vi.mock("@/components/desktop/NodeStatusPill", () => ({ NodeStatusPill: () => <span>Local tools available</span> }));
vi.mock("@/components/desktop/EngineFault", () => ({ EngineFault: () => null }));
vi.mock("@/components/desktop/DesktopUpdater", () => ({ DesktopUpdater: () => <span>Check for updates</span> }));
vi.mock("@/components/Logo", () => ({ Logo: () => <span>Lightbulb</span> }));

import { WorkspaceSidebar } from "../WorkspaceSidebar";

beforeEach(() => {
  vi.clearAllMocks();
  activePanelId = null;
  billingAccount = { subscription_status: "active" };
  isSubscribed = true;
});
afterEach(() => vi.useRealTimers());

describe("WorkspaceSidebar", () => {
  it("titles the sidebar with the active workspace, not the newest membership", () => {
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Acme" })).toBeTruthy();
    expect(screen.queryByText("Newer Workspace")).toBeNull();
    expect(screen.queryByRole("button", { name: /Acme/ })).toBeNull();
  });

  it("lets someone in two workspaces switch from the title", async () => {
    memberships = [
      { organization: { id: "org_new", name: "Newer Workspace" } },
      { organization: { id: "org_acme", name: "Acme" } },
    ];
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    try {
      render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
      const trigger = screen.getByRole("button", { name: /Acme/ });
      fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: "mouse" });
      fireEvent.click(await screen.findByRole("menuitemradio", { name: "Newer Workspace" }));
      await vi.waitFor(() => expect(setActive).toHaveBeenCalledWith({ organization: "org_new" }));
      await vi.waitFor(() => expect(assign).toHaveBeenCalledWith("/chat"));
    } finally {
      memberships = [];
      vi.unstubAllGlobals();
    }
  });

  it("renders message-first navigation with the account row and no Workspace tools block", () => {
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(screen.getAllByRole("navigation", { name: "Chats" })).toHaveLength(1);
    expect(screen.queryByText("New session")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New agent" })).not.toBeInTheDocument();
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.queryByText("Workspace tools")).not.toBeInTheDocument();
    expect(screen.queryByText("Marketplace")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Browse" })).not.toBeInTheDocument();
  });
  it("shows the Mac node status and updater only inside the desktop app, above the user row", () => {
    const { unmount } = render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(screen.queryByText("Check for updates")).not.toBeInTheDocument();
    expect(screen.queryByText("Local tools available")).not.toBeInTheDocument();
    unmount();
    (window as unknown as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke: vi.fn() } };
    try {
      render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
      const user = screen.getByText("Ada Lovelace");
      for (const text of ["Check for updates", "Local tools available"]) {
        expect(screen.getByText(text).compareDocumentPosition(user) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      }
    } finally {
      delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
    }
  });
  it.each(["dm", "channel", "group-dm", "human", "New chat or project"])("opens %s without issuing another agent selection", (label) => {
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: label }));
    expect(activateMessagesPanel).toHaveBeenCalledWith();
  });
  it("hands the drawer-close to the directory for its Invite row", () => {
    const onNavigate = vi.fn();
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: "Invite someone" }));
    expect(onNavigate).toHaveBeenCalledOnce();
  });
  it("has no More list: the task-board pages live on the Work page", () => {
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(screen.queryByText("More")).not.toBeInTheDocument();
    for (const name of ["Agents", "Money", "Projects", "Automations", "Goals", "Cases", "Accounts"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });
  it("keeps conversations while agents start", () => {
    render(<WorkspaceSidebar chatReady={false} onRecoveryReprovision={vi.fn()} />);
    // Messaging never needs the container.
    expect(screen.getByRole("navigation", { name: "Chats" })).toBeInTheDocument();
    expect(screen.queryByText(/Conversations are available when/)).not.toBeInTheDocument();
  });
  it("keeps health recovery available to free-tier owners", () => {
    isSubscribed = false;
    billingAccount = { subscription_status: "trial_local" };
    const recover = vi.fn();
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={recover} />);
    const button = screen.getByRole("button", { name: "Recover workspace" });
    // One line directly above the user row, never tucked away.
    expect(button.compareDocumentPosition(screen.getByText("Ada Lovelace")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(button);
    expect(recover).toHaveBeenCalledOnce();
  });
  it("keeps health off the footer for a workspace with no plan", () => {
    isSubscribed = false;
    billingAccount = { subscription_status: null };
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Recover workspace" })).not.toBeInTheDocument();
  });
  it.each([[86_400, "Free tier"], [-86_400, "Free tier ended"]])("preserves the free-tier footer at %s seconds from expiry", (offset, label) => {
    billingAccount = { subscription_status: "trial_local", trial_end: Math.floor(Date.now() / 1000) + offset };
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();
    if (offset > 0) expect(screen.getByRole("link", { name: "Choose a plan" })).toBeInTheDocument();
  });
});
