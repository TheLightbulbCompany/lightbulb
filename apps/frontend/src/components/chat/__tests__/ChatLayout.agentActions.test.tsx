// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";

// ---------------------------------------------------------------------------
// Header agent-action cluster: the inspector / see-work / pause-resume icon
// buttons rendered next to the file-tree button when an agent is active.
//
// Full-render (rather than source-level grep) so we assert real behaviour:
// the buttons exist when an agent is active, each fires its handler, and the
// Pause/Resume button reflects + toggles the agent's run state. The chat shell
// pulls in a lot of hooks/components, so everything irrelevant to this surface
// is stubbed below. ``useOnboardingGate`` is forced to ``ready`` so we render
// the shell (not the Loading… gate).
// ---------------------------------------------------------------------------

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    isSignedIn: true,
    isLoaded: true,
    userId: "user_1",
    getToken: vi.fn().mockResolvedValue("test-jwt"),
  }),
  useUser: () => ({ user: { id: "user_1", fullName: "Test User" }, isLoaded: true }),
  useOrganization: () => ({ organization: null, isLoaded: true }),
  useOrganizationList: () => ({
    userMemberships: { data: [] },
    userInvitations: { data: [] },
    isLoaded: true,
    setActive: vi.fn(),
  }),
  UserButton: Object.assign(() => null, {
    MenuItems: () => null,
    Link: () => null,
  }),
}));

// Force the onboarding gate ``ready`` so ChatLayout renders the shell.
vi.mock("@/hooks/useOnboardingGate", () => ({
  useOnboardingGate: () => "ready",
}));

vi.mock("@/lib/analytics", () => ({
  capture: vi.fn(),
}));

let emitChatMessage: (msg: Record<string, unknown>) => void = () => {};
let onChatMessage = (h: (m: Record<string, unknown>) => void) => {
  emitChatMessage = h;
  return () => {};
};
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    liveConnected: true,
    nodeConnected: false,
    onEvent: () => () => {},
    onChatMessage,
    sendReq: () => Promise.resolve(),
  }),
}));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: () => ({ data: { config: {} } }),
  useRuntimeRpcMutation: () => vi.fn(),
}));

// Active agent in the gateway roster (drives ``currentAgentId``). Mutable so
// the mobile-roster tests below can add a second agent and assert ordering.
let mockAgents: { id: string; name: string }[] = [{ id: "agent_1", name: "Friday" }];
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({
    agents: mockAgents,
    defaultId: "agent_1",
    createAgent: vi.fn(),
    deleteAgent: vi.fn(),
    updateAgent: vi.fn(),
  }),
  getAgentModelString: () => "",
  agentDisplayName: (a: { name?: string; id: string }) => a.name ?? a.id,
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ refresh: vi.fn(), account: null, isSubscribed: false }),
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ syncUser: vi.fn().mockResolvedValue(undefined) }),
}));
vi.mock("@/lib/modelDisplay", () => ({
  buildModelNameMap: () => ({}),
  displayModelName: () => "",
}));
vi.mock("@/lib/agentDesign", () => ({
  fromInstalledAgent: (a: unknown) => a,
}));

// ---- The surfaces under test ----
const pauseMock = vi.fn().mockResolvedValue(undefined);
const resumeMock = vi.fn().mockResolvedValue(undefined);
vi.mock("@/components/teams/agents/hooks/useAgentMutations", () => ({
  useAgentMutations: () => ({ pause: pauseMock, resume: resumeMock }),
}));

// Controllable Teams roster — each test sets ``teamsStatus`` (and optionally
// ``teamsRows``) before render.
let teamsStatus: string;
let teamsRows: { id: string; status?: string | null; role?: string | null }[] | null = null;
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({
      data: { agents: teamsRows ?? [{ id: "agent_1", status: teamsStatus }] },
    }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

// ---- Everything else: passthrough / null stubs ----
vi.mock("@/components/chat/ProvisioningStepper", () => ({
  ProvisioningStepper: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("@/components/chat/GallerySection", () => ({ GallerySection: () => null }));
vi.mock("@/components/chat/HealthIndicator", () => ({ HealthIndicator: () => null }));
vi.mock("@/components/chat/TrialBanner", () => ({ TrialBanner: () => null }));
vi.mock("@/components/chat/LaunchJoinBanner", () => ({ LaunchJoinBanner: () => null }));
vi.mock("@/components/chat/LaunchJoinModal", () => ({ LaunchJoinModal: () => null }));
vi.mock("@/components/chat/OutOfCreditsBanner", () => ({ OutOfCreditsBanner: () => null }));
vi.mock("@/components/chat/CompanyNav", () => ({ CompanyNav: () => null }));
vi.mock("@/components/chat/FileViewer", () => ({ FileViewer: () => null }));
vi.mock("@/components/chat/AgentDialogs", () => ({
  AgentCreateDialog: () => null,
  AgentRenameDialog: () => null,
  AgentDeleteDialog: () => null,
}));
vi.mock("@/components/Logo", () => ({ Logo: () => null }));
vi.mock("@/components/desktop/DesktopUpdater", () => ({ DesktopUpdater: () => null }));
vi.mock("@/components/agents/AgentIdentity", () => ({
  Monogram: () => null,
}));
vi.mock("@/components/support/SupportProvider", () => ({
  useSupport: () => ({ openSupport: vi.fn() }),
}));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    state: { freshlyInstalledAgentId: null },
    setFreshlyInstalled: vi.fn(),
  }),
}));
vi.mock("@/lib/marketplace/api", () => ({
  myListings: vi.fn().mockResolvedValue({ items: [] }),
}));
vi.mock("./ChatLayout.css", () => ({}));

import { ChatLayout } from "../ChatLayout";

const openAgentInspector = vi.fn();
const openAgentIssues = vi.fn();

function renderShell(statusIndicator?: React.ReactNode) {
  return render(
    <ChatLayout
      statusIndicator={statusIndicator}
      activePanel="overview"
      onPanelChange={vi.fn()}
      openAgentInspector={openAgentInspector}
      openAgentIssues={openAgentIssues}
      onClearPanel={vi.fn()}
      onOpenFile={vi.fn()}
    >
      <div />
    </ChatLayout>,
  );
}

beforeEach(() => {
  emitChatMessage = () => {};
  onChatMessage = (handler) => {
    emitChatMessage = handler;
    return () => {};
  };
});

describe("ChatLayout — header agent-action cluster", () => {
  beforeEach(() => {
    teamsStatus = "active";
    teamsRows = null;
    mockAgents = [{ id: "agent_1", name: "Friday" }];
    pauseMock.mockClear();
    resumeMock.mockClear();
    openAgentInspector.mockClear();
    openAgentIssues.mockClear();
  });

  it("renders the inspector / work / pause buttons when an agent is active", () => {
    renderShell();
    expect(screen.getByLabelText("Open agent inspector")).toBeTruthy();
    expect(screen.getByLabelText("See this agent's work")).toBeTruthy();
    expect(screen.getByLabelText("Pause agent")).toBeTruthy();
  });

  it("places a run indicator in the header instead of over the composer", () => {
    renderShell(<button type="button">Team is working</button>);
    const indicator = screen.getByRole("button", { name: "Team is working" });
    expect(indicator.parentElement).toContainElement(screen.getByRole("button", { name: "Get help" }));
  });

  it("opens the inspector for the active agent", () => {
    renderShell();
    fireEvent.click(screen.getByLabelText("Open agent inspector"));
    expect(openAgentInspector).toHaveBeenCalledWith("agent_1");
  });

  it("opens the agent's work view for the active agent", () => {
    renderShell();
    fireEvent.click(screen.getByLabelText("See this agent's work"));
    expect(openAgentIssues).toHaveBeenCalledWith("agent_1");
  });

  it("shows Pause and calls pause() when the agent is running", async () => {
    renderShell();
    const btn = screen.getByLabelText("Pause agent");
    fireEvent.click(btn);
    await waitFor(() => expect(pauseMock).toHaveBeenCalledWith("agent_1"));
    expect(resumeMock).not.toHaveBeenCalled();
  });

  it("reflects paused state as Resume and calls resume() when paused", async () => {
    teamsStatus = "paused";
    renderShell();
    // The Pause icon flipped to Resume because the roster reports paused.
    expect(screen.queryByLabelText("Pause agent")).toBeNull();
    const btn = screen.getByLabelText("Resume agent");
    fireEvent.click(btn);
    await waitFor(() => expect(resumeMock).toHaveBeenCalledWith("agent_1"));
    expect(pauseMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// The mobile roster. ChatLayout is the mobile-only shell and, unlike
// WorkspaceSidebar, is NOT wrapped in TeamsEventsProvider — the whole reason
// `useRosterEntries` owns its own revalidation. These mirror the desktop
// sidebar's roster tests so the two hosts can't diverge untested.
// ---------------------------------------------------------------------------
describe("ChatLayout — mobile roster", () => {
  beforeEach(() => {
    teamsStatus = "active";
    teamsRows = null;
    mockAgents = [{ id: "agent_1", name: "Friday" }];
    // The roster collapses to Friday + pins by default; these assertions are
    // about the full roster, so run it expanded.
    localStorage.setItem(
      "lightbulb:roster:prefs",
      JSON.stringify({ pinned: [], expanded: true }),
    );
  });

  const stateOf = (name: string) =>
    screen.getByText(name).closest(".agent-item")?.getAttribute("data-state");

  it("drives roster state from the Teams BFF roster", () => {
    mockAgents = [
      { id: "agent_1", name: "Friday" },
      { id: "agent_2", name: "Beam" },
      { id: "agent_3", name: "Scout" },
    ];
    teamsRows = [
      { id: "agent_1", status: "running", role: "ceo" },
      { id: "agent_2", status: "error" },
      { id: "agent_3", status: "terminated" },
    ];
    renderShell();

    expect(stateOf("Friday")).toBe("running");
    expect(stateOf("Beam")).toBe("attention");
    expect(stateOf("Scout")).toBe("terminated");
  });

  it("orders the mobile roster the same way the desktop one does", () => {
    mockAgents = [
      { id: "agent_3", name: "Scout" },
      { id: "agent_1", name: "Friday" },
      { id: "agent_2", name: "Beam" },
    ];
    teamsRows = [
      { id: "agent_1", status: "idle", role: "ceo" },
      { id: "agent_2", status: "error" },
      { id: "agent_3", status: "paused" },
    ];
    renderShell();

    expect(
      Array.from(document.querySelectorAll(".agent-item .agent-name__text")).map(
        (n) => n.textContent,
      ),
    ).toEqual(["Friday", "Beam", "Scout"]);
  });

  it("recognises `active` as live rather than bucketing it into idle", () => {
    teamsStatus = "active";
    renderShell();
    expect(stateOf("Friday")).toBe("running");
  });

  it("lights a row from a live gateway run on the mobile shell too", () => {
    teamsStatus = "idle";
    renderShell();
    expect(stateOf("Friday")).toBe("idle");

    act(() =>
      emitChatMessage({ type: "run_active", active: true, agentId: "agent_1", sessionId: "s1" }),
    );
    expect(stateOf("Friday")).toBe("running");
  });

  it("roster rows are keyboard-operable buttons on mobile as well", () => {
    teamsStatus = "idle";
    renderShell();
    expect(screen.getByRole("button", { name: "Friday" }).tagName).toBe("BUTTON");
  });
});
