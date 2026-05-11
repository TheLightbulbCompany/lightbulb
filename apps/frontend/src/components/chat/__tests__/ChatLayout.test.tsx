// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

let clerkState: {
  isSignedIn: boolean;
  user: { unsafeMetadata?: Record<string, unknown>; fullName?: string; firstName?: string } | null;
  organization: { id: string } | null;
  memberships: unknown[];
  invitations: unknown[];
};

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    isSignedIn: clerkState.isSignedIn,
    isLoaded: true,
    getToken: vi.fn().mockResolvedValue("test-jwt"),
  }),
  useUser: () => ({ user: clerkState.user, isLoaded: true }),
  useOrganization: () => ({
    organization: clerkState.organization,
    isLoaded: true,
  }),
  useOrganizationList: () => ({
    userMemberships: { data: clerkState.memberships },
    userInvitations: { data: clerkState.invitations },
    isLoaded: true,
    setActive: vi.fn(),
  }),
  UserButton: () => null,
}));

// Stub the heavy hooks ChatLayout pulls in — irrelevant to the routing test.
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
  // ``lib/analytics`` re-exports PostHogProvider as ``Provider`` at module
  // load time, so the mock has to expose it (vitest is strict about
  // import-time destructuring against module mocks). Returning a passthrough
  // component is enough — these tests don't render the chat shell into
  // PostHog's actual provider.
  PostHogProvider: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    nodeConnected: false,
    // useExecApprovals (mounted by ChatLayout) subscribes to onEvent and
    // calls sendReq on resolve, so the mock has to satisfy both.
    onEvent: () => () => {},
    sendReq: () => Promise.resolve(),
  }),
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
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    refresh: vi.fn(),
    account: null,
    isSubscribed: false,
  }),
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ syncUser: vi.fn().mockResolvedValue(undefined) }),
}));

// JSX dependencies — only matter if we don't hit the early Loading... return,
// but stub them anyway so the rendered tree never crashes.
vi.mock("@/components/chat/ProvisioningStepper", () => ({
  ProvisioningStepper: () => null,
}));
vi.mock("@/components/chat/GallerySection", () => ({
  GallerySection: () => null,
}));
vi.mock("@/components/chat/HealthIndicator", () => ({
  HealthIndicator: () => null,
}));
vi.mock("@/components/chat/TrialBanner", () => ({
  TrialBanner: () => null,
}));
vi.mock("@/components/chat/OutOfCreditsBanner", () => ({
  OutOfCreditsBanner: () => null,
}));
vi.mock("@/components/control/ControlSidebar", () => ({
  ControlSidebar: () => null,
}));
vi.mock("@/components/chat/FileViewer", () => ({
  FileViewer: () => null,
}));
vi.mock("@/components/chat/AgentDialogs", () => ({
  AgentCreateDialog: () => null,
  AgentRenameDialog: () => null,
  AgentDeleteDialog: () => null,
}));
// Drawer provider is mounted on the chat page in production, but this test
// renders <ChatLayout /> bare. Stub the hook so we don't have to wrap.
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    state: { freshlyInstalledAgentId: null },
    setFreshlyInstalled: vi.fn(),
  }),
}));
// myListings (Task 17): the sidebar fetches this on mount to compute the
// gold pub-dot set; default to an empty list so no badge renders in the test.
vi.mock("@/lib/marketplace/api", () => ({
  myListings: vi.fn().mockResolvedValue({ items: [] }),
}));
// CSS import is a no-op in vitest jsdom env, but stub it for safety.
vi.mock("./ChatLayout.css", () => ({}));

describe("ChatLayout — needsInvitationFlow", () => {
  beforeEach(() => {
    replaceMock.mockReset();
    vi.resetModules();
  });

  it("redirects an already-onboarded user with a pending invite to /onboarding", async () => {
    clerkState = {
      isSignedIn: true,
      // load-bearing: this used to block the redirect under the old logic
      user: { unsafeMetadata: { onboarded: true }, fullName: "Test User" },
      organization: null,
      memberships: [],
      invitations: [{ id: "orginv_1" }],
    };
    const { ChatLayout } = await import("../ChatLayout");
    render(
      <ChatLayout activeView="chat" onViewChange={() => {}}>
        <div />
      </ChatLayout>,
    );
    expect(replaceMock).toHaveBeenCalledWith("/onboarding");
  });

  it("does NOT redirect when user has memberships (already in an org)", async () => {
    clerkState = {
      isSignedIn: true,
      user: { unsafeMetadata: { onboarded: true }, fullName: "Test User" },
      organization: { id: "org_a" },
      memberships: [{ organization: { id: "org_a" } }],
      invitations: [],
    };
    const { ChatLayout } = await import("../ChatLayout");
    render(
      <ChatLayout activeView="chat" onViewChange={() => {}}>
        <div />
      </ChatLayout>,
    );
    expect(replaceMock).not.toHaveBeenCalledWith("/onboarding");
  });
});
