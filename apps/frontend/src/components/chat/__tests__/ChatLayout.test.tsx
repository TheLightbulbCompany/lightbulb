// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen } from "@testing-library/react";

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

let clerkState: {
  isSignedIn: boolean;
  user: {
    id?: string;
    unsafeMetadata?: Record<string, unknown>;
    fullName?: string;
    firstName?: string;
  } | null;
  organization: { id: string } | null;
  memberships: unknown[];
  invitations: unknown[];
};

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    isSignedIn: clerkState.isSignedIn,
    isLoaded: true,
    userId: clerkState.user?.id ?? null,
    orgId: clerkState.organization?.id ?? null,
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
    setActive: vi.fn().mockResolvedValue(undefined),
  }),
  UserButton: () => null,
  OrganizationList: () => null,
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
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: undefined }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    nodeConnected: false,
    // ChatLayout reads nodeConnected directly and calls
    // useRuntimeRpc("config.get"), whose shared implementation destructures
    // sendReq and subscribes to onEvent for cache invalidation — the mock
    // has to satisfy all three. (ExecApprovalsProvider no longer mounts
    // here; it was hoisted to the /chat page shell.)
    onEvent: () => () => {},
    onChatMessage: () => () => {},
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
vi.mock("@/components/support/SupportProvider", () => ({
  useSupport: () => ({ openSupport: vi.fn() }),
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
vi.mock("@/components/chat/LaunchJoinBanner", () => ({
  LaunchJoinBanner: () => <div data-testid="launch-join-banner" />,
}));
vi.mock("@/components/chat/LaunchJoinModal", () => ({
  LaunchJoinModal: () => null,
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

  it("redirects a user with a pending invite to /onboarding", async () => {
    clerkState = {
      isSignedIn: true,
      user: { fullName: "Test User" },
      organization: null,
      memberships: [],
      invitations: [{ id: "orginv_1" }],
    };
    const { ChatLayout } = await import("../ChatLayout");
    await act(async () => { render(
      <ChatLayout>
        <div />
      </ChatLayout>,
    ); });
    expect(replaceMock).toHaveBeenCalledWith("/onboarding");
  });

  it("does NOT redirect when user has memberships (already in an org)", async () => {
    clerkState = {
      isSignedIn: true,
      user: { fullName: "Test User" },
      organization: { id: "org_a" },
      memberships: [{ organization: { id: "org_a" } }],
      invitations: [],
    };
    const { ChatLayout } = await import("../ChatLayout");
    await act(async () => { render(
      <ChatLayout>
        <div />
      </ChatLayout>,
    ); });
    expect(replaceMock).not.toHaveBeenCalledWith("/onboarding");

  });

  it("shows workspace selection for multiple memberships without an active workspace", async () => {
    clerkState = { isSignedIn: true, user: { id: "user_1" }, organization: null,
      memberships: [{ organization: { id: "org_a" } }, { organization: { id: "org_b" } }], invitations: [] };
    const { ChatLayout } = await import("../ChatLayout");
    await act(async () => { render(<ChatLayout><div /></ChatLayout>); });
    expect(screen.getByRole("heading", { name: "Choose your workspace" })).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });
});

describe("ChatLayout — run toast opens a room", () => {
  it("drops ?panel on openRoom so the selected room is visible", async () => {
    clerkState = { isSignedIn: true, user: { id: "user_1" }, organization: { id: "org_a" },
      memberships: [{ organization: { id: "org_a" } }], invitations: [] };
    const { ChatLayout } = await import("../ChatLayout");
    const onClearPanel = vi.fn();
    await act(async () => { render(<ChatLayout onClearPanel={onClearPanel}><div /></ChatLayout>); });
    act(() => { window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId: "r1", threadId: null } })); });
    expect(onClearPanel).toHaveBeenCalledTimes(1);
  });
});
