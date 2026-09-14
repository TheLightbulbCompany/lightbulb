// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, within } from "@testing-library/react";
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
  MessagingDirectory: ({ onOpenRoom }: { onOpenRoom: (room?: DirectoryRoom) => void }) => (
    <nav aria-label="Conversations">
      {(["dm", "channel", "group-dm", "human"] as const).map((kind) => (
        <button key={kind} onClick={() => onOpenRoom({
          roomId: kind, name: null, roomRevision: 1, lastMessageSeq: 0,
          lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z",
          topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0,
          kind: kind === "human" ? "dm" : kind,
          participants: [{ kind: kind === "human" ? "human" : "agent", id: "agent-1", displayName: "Counterpart", mentionHandle: null }],
        })}>{kind}</button>
      ))}
      <button onClick={() => onOpenRoom()}>New conversation</button>
    </nav>
  ),
}));
let billingAccount: { subscription_status: string | null; trial_end?: number | null };
let isSubscribed: boolean;
let provisioningEnabled: boolean;
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ account: billingAccount, isSubscribed }) }));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => ({ data: { provisioning_enabled: provisioningEnabled } }),
}));
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({ user: { fullName: "Ada Lovelace" } }),
}));
let freshlyInstalledAgentId: string | null;
const setFreshlyInstalled = vi.fn();
const openMarketplace = vi.fn();
const emitStep = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ state: { freshlyInstalledAgentId }, setFreshlyInstalled, open: openMarketplace }),
}));
vi.mock("@/components/getstarted/getStartedEvents", () => ({ emitStep: (...args: unknown[]) => emitStep(...args) }));
vi.mock("@/components/chat/HealthIndicator", () => ({
  HealthIndicator: ({ onRecoveryReprovision }: { onRecoveryReprovision: () => void }) => (
    <button onClick={onRecoveryReprovision}>Recover workspace</button>
  ),
}));
vi.mock("@/components/desktop/NodeStatusPill", () => ({ NodeStatusPill: () => <span>Local tools available</span> }));
vi.mock("@/components/desktop/DesktopUpdater", () => ({ DesktopUpdater: () => <span>Check for updates</span> }));
vi.mock("@/components/Logo", () => ({ Logo: () => <span>Lightbulb</span> }));

import { WorkspaceSidebar } from "../WorkspaceSidebar";

beforeEach(() => {
  vi.clearAllMocks();
  activePanelId = null;
  billingAccount = { subscription_status: "active" };
  isSubscribed = true;
  provisioningEnabled = false;
  freshlyInstalledAgentId = null;
});
afterEach(() => vi.useRealTimers());

describe("WorkspaceSidebar", () => {
  it("renders message-first navigation and preserves marketplace and account exits", () => {
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(screen.getAllByRole("navigation", { name: "Conversations" })).toHaveLength(1);
    expect(screen.queryByText("New session")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New agent" })).not.toBeInTheDocument();
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Workspace tools"));
    expect(screen.getByText("Check for updates")).toBeInTheDocument();
    expect(screen.getByText("Local tools available")).toBeInTheDocument();
    expect(screen.getByText("Marketplace")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Browse" }));
    expect(openMarketplace).toHaveBeenCalledWith({ view: "browse" });
  });
  it.each(["dm", "channel", "group-dm", "human", "New conversation"])("opens %s without issuing another agent selection", (label) => {
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: label }));
    expect(activateMessagesPanel).toHaveBeenCalledWith();
  });
  it("keeps secondary destinations in native More and respects capability filters", () => {
    const view = render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    const details = screen.getByText("More").closest("details")!;
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("More"));
    expect(details).toHaveAttribute("open");
    expect(within(details).getAllByRole("button").map((button) => button.textContent)).toEqual(["Agents", "Money", "Automations", "Leads", "Library", "Accounts"]);
    fireEvent.click(within(details).getByRole("button", { name: "Agents" }));
    expect(openPanel).toHaveBeenCalledWith("teams-agents", {}, { pushUrl: true });
    expect(screen.queryByText("Domain & Site")).not.toBeInTheDocument();
    provisioningEnabled = true;
    view.rerender(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    fireEvent.click(within(details).getByRole("button", { name: "Domain & Site" }));
    expect(openPanel).toHaveBeenCalledWith("teams-domain-site", {}, { pushUrl: true });
  });
  it("disables container-backed destinations but keeps conversations while agents start", () => {
    render(<WorkspaceSidebar chatReady={false} onRecoveryReprovision={vi.fn()} />);
    fireEvent.click(screen.getByText("More"));
    for (const name of ["Agents", "Money"]) {
      const button = screen.getByRole("button", { name });
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    expect(openPanel).not.toHaveBeenCalled();
    // Messaging never needs the container.
    expect(screen.getByRole("navigation", { name: "Conversations" })).toBeInTheDocument();
    expect(screen.queryByText(/Conversations are available when/)).not.toBeInTheDocument();
  });
  it("keeps health recovery available to free-tier owners", () => {
    isSubscribed = false;
    billingAccount = { subscription_status: "trial_local" };
    const recover = vi.fn();
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={recover} />);
    fireEvent.click(screen.getByText("Workspace tools"));
    fireEvent.click(screen.getByRole("button", { name: "Recover workspace" }));
    expect(recover).toHaveBeenCalledOnce();
  });
  it("records completed marketplace hires and clears the installation marker", () => {
    vi.useFakeTimers();
    freshlyInstalledAgentId = "agent-new";
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(emitStep).toHaveBeenCalledWith("hire");
    act(() => vi.advanceTimersByTime(600));
    expect(setFreshlyInstalled).toHaveBeenCalledWith(null);
  });
  it.each([[86_400, "Free tier"], [-86_400, "Free tier ended"]])("preserves the free-tier footer at %s seconds from expiry", (offset, label) => {
    billingAccount = { subscription_status: "trial_local", trial_end: Math.floor(Date.now() / 1000) + offset };
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();
    if (offset > 0) expect(screen.getByRole("link", { name: "See plans" })).toBeInTheDocument();
  });
});
