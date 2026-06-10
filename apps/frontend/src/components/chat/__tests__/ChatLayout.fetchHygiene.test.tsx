// @vitest-environment jsdom
/**
 * Per-load fetch hygiene for the chat shell: seller listings and
 * users/sync must fire once per session, not once per mount. ChatLayout
 * remounts on view switches and agent-key changes, and every page load
 * was re-firing both calls.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor } from "@testing-library/react";

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    isSignedIn: true,
    isLoaded: true,
    userId: "user_1",
    getToken: vi.fn().mockResolvedValue("test-jwt"),
  }),
  useUser: () => ({
    user: { id: "user_1", unsafeMetadata: { onboarded: true }, fullName: "Test User" },
    isLoaded: true,
  }),
  useOrganization: () => ({ organization: null, isLoaded: true }),
  useOrganizationList: () => ({
    userMemberships: { data: [] },
    userInvitations: { data: [] },
    isLoaded: true,
    setActive: vi.fn(),
  }),
  UserButton: () => null,
}));

// The fetches under test are gated on the onboarding gate being ready.
vi.mock("@/hooks/useOnboardingGate", () => ({
  useOnboardingGate: () => "ready",
}));

vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
  PostHogProvider: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    nodeConnected: false,
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
  useBilling: () => ({ refresh: vi.fn(), account: null, isSubscribed: true }),
}));

const syncUserMock = vi.fn().mockResolvedValue(undefined);
vi.mock("@/lib/api", () => ({
  useApi: () => ({ syncUser: syncUserMock }),
}));

vi.mock("@/components/chat/ProvisioningStepper", () => ({
  ProvisioningStepper: ({ children }: { children?: React.ReactNode }) => children ?? null,
}));
vi.mock("@/components/chat/GallerySection", () => ({ GallerySection: () => null }));
vi.mock("@/components/chat/HealthIndicator", () => ({ HealthIndicator: () => null }));
vi.mock("@/components/chat/TrialBanner", () => ({ TrialBanner: () => null }));
vi.mock("@/components/chat/OutOfCreditsBanner", () => ({ OutOfCreditsBanner: () => null }));
vi.mock("@/components/control/ControlSidebar", () => ({ ControlSidebar: () => null }));
vi.mock("@/components/chat/FileViewer", () => ({ FileViewer: () => null }));
vi.mock("@/components/chat/AgentDialogs", () => ({
  AgentCreateDialog: () => null,
  AgentRenameDialog: () => null,
  AgentDeleteDialog: () => null,
}));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    state: { freshlyInstalledAgentId: null },
    setFreshlyInstalled: vi.fn(),
  }),
}));

const myListingsMock = vi.fn().mockResolvedValue({ items: [] });
vi.mock("@/lib/marketplace/api", () => ({
  myListings: (...args: unknown[]) => myListingsMock(...args),
}));
vi.mock("./ChatLayout.css", () => ({}));

import { ChatLayout } from "../ChatLayout";

function renderLayout() {
  return render(
    <ChatLayout activeView="chat" onViewChange={() => {}}>
      <div />
    </ChatLayout>,
  );
}

describe("ChatLayout — per-load fetch hygiene", () => {
  beforeEach(() => {
    // The global test setup runs vi.restoreAllMocks() after each test,
    // which strips mockResolvedValue implementations — re-arm them here.
    myListingsMock.mockReset().mockResolvedValue({ items: [] });
    syncUserMock.mockReset().mockResolvedValue(undefined);
    sessionStorage.clear();
  });

  it("fetches seller listings once across remounts, not once per mount", async () => {
    const first = renderLayout();
    await waitFor(() => expect(myListingsMock).toHaveBeenCalledTimes(1));
    first.unmount();

    renderLayout();
    // Allow any (incorrect) refetch to land before asserting.
    await new Promise((r) => setTimeout(r, 50));
    expect(myListingsMock).toHaveBeenCalledTimes(1);
  });

  it("fires users/sync once per session across remounts", async () => {
    const first = renderLayout();
    await waitFor(() => expect(syncUserMock).toHaveBeenCalledTimes(1));
    first.unmount();

    renderLayout();
    await new Promise((r) => setTimeout(r, 50));
    expect(syncUserMock).toHaveBeenCalledTimes(1);
  });
});
