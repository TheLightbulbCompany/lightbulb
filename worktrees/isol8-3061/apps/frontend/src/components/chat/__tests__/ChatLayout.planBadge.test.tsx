// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

// ---------------------------------------------------------------------------
// Footer .plan-badge: must map subscription_status through planLabel(), never
// render the raw enum ("trial_local" would come out "TRIAL_LOCAL" — the
// footer is CSS text-transform: uppercase), and must agree with
// TrialBanner/OutOfCreditsBanner elsewhere on this same screen about whether
// the free-tier window has elapsed.
//
// Full-render (not source grep) so the planLabel(subscriptionStatus, {
// expired }) wiring is actually exercised. Mocking harness follows
// ChatLayout.agentActions.test.tsx's pattern: force useOnboardingGate
// "ready" so the shell renders past the Loading… gate, stub everything
// irrelevant to the footer.
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

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    nodeConnected: false,
    onEvent: () => () => {},
    onChatMessage: () => () => {},
    sendReq: () => Promise.resolve(),
  }),
}));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: () => ({ data: { config: {} } }),
  useRuntimeRpcMutation: () => vi.fn(),
}));

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({
    agents: [],
    defaultId: null,
    createAgent: vi.fn(),
    deleteAgent: vi.fn(),
    updateAgent: vi.fn(),
  }),
  getAgentModelString: () => "",
  agentDisplayName: () => "Agent",
}));

// Mutable so each test can set subscription_status/trial_end.
let billingAccount: { subscription_status: string | null; trial_end?: number | null } | null =
  null;
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ refresh: vi.fn(), account: billingAccount, isSubscribed: false }),
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
vi.mock("@/components/teams/agents/hooks/useAgentMutations", () => ({
  useAgentMutations: () => ({ pause: vi.fn(), resume: vi.fn() }),
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { agents: [] } }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

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

function renderShell() {
  return render(
    <ChatLayout activePanel="overview" onPanelChange={vi.fn()}>
      <div />
    </ChatLayout>,
  );
}

describe("ChatLayout — footer plan-badge", () => {
  beforeEach(() => {
    billingAccount = null;
  });

  it("renders nothing when there is no billing account yet", () => {
    renderShell();
    expect(document.querySelector(".plan-badge")).toBeNull();
  });

  it("maps trial_local to Free tier, never the raw enum", () => {
    billingAccount = {
      subscription_status: "trial_local",
      trial_end: Math.floor(Date.now() / 1000) + 86_400, // 1 day out — not expired
    };
    renderShell();
    expect(screen.getByText("Free tier")).toBeInTheDocument();
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();
  });

  it("reads Free tier ended once the free-tier window has elapsed", () => {
    billingAccount = {
      subscription_status: "trial_local",
      trial_end: Math.floor(Date.now() / 1000) - 86_400, // 1 day ago — expired
    };
    renderShell();
    expect(screen.getByText("Free tier ended")).toBeInTheDocument();
    expect(screen.queryByText("Free tier")).not.toBeInTheDocument();
  });

  it("capitalizes other statuses via planLabel", () => {
    billingAccount = { subscription_status: "active" };
    renderShell();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });
});
