// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

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

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    nodeConnected: false,
    onEvent: () => () => {},
    sendReq: () => Promise.resolve(),
  }),
}));
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({ data: { config: {} } }),
  useGatewayRpcMutation: () => vi.fn(),
}));

// Active agent in the gateway roster (drives ``currentAgentId``).
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({
    agents: [{ id: "agent_1", name: "Friday" }],
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

// Controllable Teams roster status — each test sets ``teamsStatus`` before render.
let teamsStatus: string;
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { agents: [{ id: "agent_1", status: teamsStatus }] } }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

// ---- Everything else: passthrough / null stubs ----
vi.mock("@/hooks/useExecApprovals", () => ({
  ExecApprovalsProvider: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("@/components/chat/ProvisioningStepper", () => ({
  ProvisioningStepper: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("@/components/chat/GallerySection", () => ({ GallerySection: () => null }));
vi.mock("@/components/chat/HealthIndicator", () => ({ HealthIndicator: () => null }));
vi.mock("@/components/chat/TrialBanner", () => ({ TrialBanner: () => null }));
vi.mock("@/components/chat/OutOfCreditsBanner", () => ({ OutOfCreditsBanner: () => null }));
vi.mock("@/components/chat/CompanyNav", () => ({ CompanyNav: () => null }));
vi.mock("@/components/chat/FileViewer", () => ({ FileViewer: () => null }));
vi.mock("@/components/chat/AgentDialogs", () => ({
  AgentCreateDialog: () => null,
  AgentRenameDialog: () => null,
  AgentDeleteDialog: () => null,
  SessionDeleteDialog: () => null,
}));
vi.mock("@/components/Logo", () => ({ Logo: () => null }));
vi.mock("@/components/desktop/DesktopUpdater", () => ({ DesktopUpdater: () => null }));
vi.mock("@/components/agents/AgentIdentity", () => ({
  Monogram: () => null,
  StatusDot: () => null,
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

function renderShell() {
  return render(
    <ChatLayout
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

describe("ChatLayout — header agent-action cluster", () => {
  beforeEach(() => {
    teamsStatus = "active";
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
