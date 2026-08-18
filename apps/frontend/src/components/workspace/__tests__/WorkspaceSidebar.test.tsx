// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

// --- Mocks -------------------------------------------------------------------
// The controller is the whole point of this component: roster clicks and
// CompanyNav changes must route THROUGH it (never a local userSelectedId),
// and the active row derives from the store snapshot. Mock it so we assert
// wiring, not dockview internals.
const openChatPanel = vi.fn();
const openPanel = vi.fn();
const closeChatPanelIfOpen = vi.fn();
let snapshot: { activePanelId: string | null; focusedChatAgentId: string | null };
vi.mock("../workspaceController", () => ({
  openChatPanel: (...a: unknown[]) => openChatPanel(...a),
  openPanel: (...a: unknown[]) => openPanel(...a),
  closeChatPanelIfOpen: (...a: unknown[]) => closeChatPanelIfOpen(...a),
  useWorkspaceState: () => snapshot,
}));

const capture = vi.fn();
vi.mock("@/lib/analytics", () => ({ capture: (...a: unknown[]) => capture(...a) }));

interface FakeAgent {
  id: string;
  name?: string;
  identity?: { name?: string };
  model?: unknown;
}
let agents: FakeAgent[];
let defaultId: string | undefined;
const createAgent = vi.fn(async () => {});
const deleteAgent = vi.fn(async () => {});
const updateAgent = vi.fn(async () => {});
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents, defaultId, createAgent, deleteAgent, updateAgent }),
  agentDisplayName: (a: FakeAgent) => a.identity?.name || a.name || a.id,
  getAgentModelString: () => "",
}));
vi.mock("@/lib/modelDisplay", () => ({
  buildModelNameMap: () => ({}),
  displayModelName: () => null,
}));

vi.mock("@/hooks/useGatewayRpc", () => ({ useGatewayRpc: () => ({ data: undefined }) }));
// `useRosterEntries` subscribes to the chat-frame fan-out for live `run_active`
// frames. `emitChatMessage` lets a test push one and assert the row lights up.
let emitChatMessage: (msg: Record<string, unknown>) => void = () => {};
vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    nodeConnected: false,
    onChatMessage: (h: (m: Record<string, unknown>) => void) => {
      emitChatMessage = h;
      return () => {};
    },
  }),
}));
// Durable per-agent state from the Teams BFF roster; tests set it per case.
let teamsAgents: { id: string; status?: string | null; role?: string; seeded?: boolean }[] = [];
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { agents: teamsAgents } }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));
// Mutable so the plan-badge tests below can swap subscription_status/trial_end
// per case; every other test in this file relies on the "active" default set
// in beforeEach.
let billingAccount: { subscription_status: string | null; trial_end?: number | null } = {
  subscription_status: "active",
};
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: billingAccount, isSubscribed: true }),
}));

const selectSession = vi.fn();
const newSession = vi.fn();
vi.mock("@/hooks/useActiveSession", () => ({
  useActiveSession: () => ({ activeSessionId: null, newSession, selectSession }),
}));
vi.mock("@/hooks/useAgentSessions", () => ({
  useAgentSessions: () => ({ sessions: [], renameSession: vi.fn(), deleteSession: vi.fn() }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "user_1" }),
  useUser: () => ({ user: { fullName: "Ada Lovelace" } }),
}));

vi.mock("@/components/chat/shell/useSellerPublishedAgents", () => ({
  useSellerPublishedAgents: () => new Set<string>(),
}));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    state: { freshlyInstalledAgentId: null },
    setFreshlyInstalled: vi.fn(),
  }),
}));
vi.mock("@/components/getstarted/getStartedEvents", () => ({ emitStep: vi.fn() }));
vi.mock("@/components/chat/InlineTeamsPanel", () => ({ TEAMS_PANEL_PREFIX: "teams-" }));

// Heavy children reduced to probes.
vi.mock("@/components/agents/AgentIdentity", () => ({
  Monogram: () => <span data-testid="monogram" />,
}));
vi.mock("@/lib/agentDesign", () => ({ fromInstalledAgent: (a: unknown) => a }));
vi.mock("@/components/chat/HealthIndicator", () => ({
  HealthIndicator: ({ onRecoveryReprovision }: { onRecoveryReprovision?: () => void }) => (
    <button data-testid="health" onClick={() => onRecoveryReprovision?.()} />
  ),
}));
vi.mock("@/components/chat/GallerySection", () => ({
  GallerySection: () => <div data-testid="gallery" />,
}));
vi.mock("@/components/desktop/DesktopUpdater", () => ({
  DesktopUpdater: () => <div data-testid="updater" />,
}));
vi.mock("@/components/Logo", () => ({ Logo: () => <div data-testid="logo" /> }));
vi.mock("@/components/chat/SessionList", () => ({
  SessionList: () => <div data-testid="session-list" />,
}));
vi.mock("@/components/chat/AgentDialogs", () => ({
  AgentCreateDialog: ({ open }: { open: boolean }) =>
    open ? <div data-testid="create-dialog" /> : null,
  AgentRenameDialog: ({ agent }: { agent: unknown }) =>
    agent ? <div data-testid="rename-dialog" /> : null,
  AgentDeleteDialog: ({ agent }: { agent: unknown }) =>
    agent ? <div data-testid="delete-dialog" /> : null,
  SessionDeleteDialog: () => null,
}));
// CompanyNav reduced to a probe exposing the activePanel prop it received and a
// button that fires a change — lets us assert store→prop passthrough AND the
// openPanel wiring without pulling the real teams tree.
vi.mock("@/components/chat/CompanyNav", () => ({
  CompanyNav: ({
    activePanel,
    onPanelChange,
  }: {
    activePanel?: string;
    onPanelChange?: (p: string) => void;
  }) => (
    <button
      data-testid="companynav"
      data-active-panel={activePanel ?? ""}
      onClick={() => onPanelChange?.("teams-inbox")}
    />
  ),
}));

import { WorkspaceSidebar } from "../WorkspaceSidebar";

beforeEach(() => {
  agents = [];
  defaultId = undefined;
  teamsAgents = [];
  snapshot = { activePanelId: null, focusedChatAgentId: null };
  billingAccount = { subscription_status: "active" };
  openChatPanel.mockClear();
  openPanel.mockClear();
  closeChatPanelIfOpen.mockClear();
  capture.mockClear();
  selectSession.mockClear();
  newSession.mockClear();
});

describe("WorkspaceSidebar", () => {
  it("roster row click opens the chat panel via the controller + captures", () => {
    agents = [{ id: "agent-ceo", name: "Friday" }];
    defaultId = "agent-ceo";
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    fireEvent.click(screen.getByText("Friday"));

    expect(openChatPanel).toHaveBeenCalledWith("agent-ceo");
    expect(capture).toHaveBeenCalledWith("agent_selected", { agent_id: "agent-ceo" });
  });

  it("marks the active row from the store snapshot (focusedChatAgentId)", () => {
    agents = [
      { id: "a1", name: "One" },
      { id: "a2", name: "Two" },
    ];
    snapshot = { activePanelId: "chat:a2", focusedChatAgentId: "a2" };
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    expect(screen.getByText("Two").closest(".agent-item")?.className).toContain("active");
    expect(screen.getByText("One").closest(".agent-item")?.className).not.toContain("active");
  });

  it("CompanyNav change routes through openPanel with pushUrl", () => {
    snapshot = { activePanelId: "teams-inbox", focusedChatAgentId: null };
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    const nav = screen.getByTestId("companynav");
    // Store's teams panel id flows through to CompanyNav's activePanel.
    expect(nav.getAttribute("data-active-panel")).toBe("teams-inbox");

    fireEvent.click(nav);
    expect(openPanel).toHaveBeenCalledWith("teams-inbox", {}, { pushUrl: true });
  });

  it("passes activePanel=undefined to CompanyNav when the active panel is not a teams id", () => {
    snapshot = { activePanelId: "agent-inspector", focusedChatAgentId: null };
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    expect(screen.getByTestId("companynav").getAttribute("data-active-panel")).toBe("");
  });

  it("Task create-pill routes to the tasks panel with the new=task one-shot", () => {
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    fireEvent.click(screen.getByLabelText("New task"));
    expect(openPanel).toHaveBeenCalledWith(
      "teams-issues",
      {},
      { pushUrl: true, oneShots: { new: "task" } },
    );
  });

  it("create-pill buttons are disabled while the workspace is provisioning", () => {
    render(<WorkspaceSidebar chatReady={false} onRecoveryReprovision={vi.fn()} />);

    expect(screen.getByLabelText("New agent")).toBeDisabled();
    expect(screen.getByLabelText("New task")).toBeDisabled();

    fireEvent.click(screen.getByLabelText("New task"));
    expect(openPanel).not.toHaveBeenCalled();
  });

  it("per-row action buttons deep-link into inspector + tasks via openPanel", () => {
    agents = [{ id: "agent-ceo", name: "Friday" }];
    defaultId = "agent-ceo";
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    fireEvent.click(screen.getByLabelText("Inspect Friday"));
    expect(openPanel).toHaveBeenCalledWith(
      "agent-inspector",
      { agent: "agent-ceo" },
      { pushUrl: true },
    );

    fireEvent.click(screen.getByLabelText("See Friday's work"));
    expect(openPanel).toHaveBeenCalledWith(
      "teams-issues",
      { agent: "agent-ceo" },
      { pushUrl: true },
    );
  });

  it("HealthIndicator recovery bubbles to the onRecoveryReprovision prop", () => {
    const onRecoveryReprovision = vi.fn();
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={onRecoveryReprovision} />);

    fireEvent.click(screen.getByTestId("health"));
    expect(onRecoveryReprovision).toHaveBeenCalledTimes(1);
  });

  it("drives roster state from the Teams BFF roster, not a hardcoded dot", () => {
    agents = [
      { id: "a1", name: "One" },
      { id: "a2", name: "Two" },
      { id: "a3", name: "Three" },
    ];
    teamsAgents = [
      { id: "a1", status: "running" },
      { id: "a2", status: "error" },
      { id: "a3", status: "idle" },
    ];
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    const state = (n: string) =>
      screen.getByText(n).closest(".agent-item")?.getAttribute("data-state");
    expect(state("One")).toBe("running");
    expect(state("Two")).toBe("attention");
    expect(state("Three")).toBe("idle");
    // An errored agent is hoisted; a running one is not (it would slide the
    // row out from under the cursor for a transient condition).
    expect(
      Array.from(document.querySelectorAll(".agent-item .agent-name__text")).map(
        (n) => n.textContent,
      ),
    ).toEqual(["Two", "One", "Three"]);
  });

  it("lights a row from a live gateway run when the BFF still says idle", () => {
    agents = [{ id: "a1", name: "One" }];
    teamsAgents = [{ id: "a1", status: "idle" }];
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);
    expect(screen.getByText("One").closest(".agent-item")?.getAttribute("data-state")).toBe("idle");

    act(() => emitChatMessage({ type: "run_active", active: true, agent_id: "a1" }));
    expect(screen.getByText("One").closest(".agent-item")?.getAttribute("data-state")).toBe(
      "running",
    );

    // `run_ended` clears it even if the matching `run_active: false` never lands.
    act(() => emitChatMessage({ type: "run_ended", agent_id: "a1" }));
    expect(screen.getByText("One").closest(".agent-item")?.getAttribute("data-state")).toBe("idle");
  });

  it("pins the CEO first and marks her rank, from the Paperclip role", () => {
    agents = [
      { id: "a1", name: "Zeta" },
      { id: "friday", name: "Friday" },
    ];
    teamsAgents = [
      { id: "a1", status: "idle" },
      { id: "friday", status: "idle", role: "ceo" },
    ];
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    const rows = Array.from(document.querySelectorAll(".agent-item .agent-name__text")).map(
      (n) => n.textContent,
    );
    expect(rows).toEqual(["Friday", "Zeta"]);
    expect(screen.getByText("Friday").closest(".agent-item")?.getAttribute("data-ceo")).toBe("true");
    expect(screen.getByText("CEO")).toBeInTheDocument();
  });

  it("makes no CEO claim when the container's default agent has no ceo role", () => {
    // `_build_agents_list` falls back to index 0 when nothing carries the
    // role, so `defaultId` is an arbitrary agent on a legacy company. The
    // badge must not outrun the data — it only hides the delete button.
    agents = [
      { id: "a1", name: "Zeta" },
      { id: "a2", name: "Alpha" },
    ];
    defaultId = "a1";
    teamsAgents = [
      { id: "a1", status: "idle" },
      { id: "a2", status: "idle" },
    ];
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    expect(screen.queryByText("CEO")).not.toBeInTheDocument();
    expect(document.querySelector('.agent-item[data-ceo="true"]')).toBeNull();
    // Nobody pinned → plain name order.
    expect(
      Array.from(document.querySelectorAll(".agent-item .agent-name__text")).map(
        (n) => n.textContent,
      ),
    ).toEqual(["Alpha", "Zeta"]);
  });

  it("roster rows are keyboard-operable buttons, not bare click divs", () => {
    agents = [{ id: "a1", name: "One" }];
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    const row = screen.getByRole("button", { name: "One" });
    expect(row.tagName).toBe("BUTTON");
    fireEvent.click(row);
    expect(openChatPanel).toHaveBeenCalledWith("a1");
  });

  it("footer plan-badge maps trial_local to Free tier, never the raw enum", () => {
    billingAccount = {
      subscription_status: "trial_local",
      trial_end: Math.floor(Date.now() / 1000) + 86_400, // 1 day out — not expired
    };
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    expect(screen.getByText("Free tier")).toBeInTheDocument();
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();
  });

  it("footer plan-badge reads Free tier ended once the free-tier window has elapsed", () => {
    billingAccount = {
      subscription_status: "trial_local",
      trial_end: Math.floor(Date.now() / 1000) - 86_400, // 1 day ago — expired
    };
    render(<WorkspaceSidebar chatReady onRecoveryReprovision={vi.fn()} />);

    expect(screen.getByText("Free tier ended")).toBeInTheDocument();
    expect(screen.queryByText("Free tier")).not.toBeInTheDocument();
  });
});
