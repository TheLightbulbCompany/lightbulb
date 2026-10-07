// @vitest-environment jsdom
/**
 * Per-load fetch hygiene for the chat shell: seller listings and
 * users/sync must fire once per session, not once per mount. ChatLayout
 * remounts on view switches and agent-key changes, and every page load
 * was re-firing both calls.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, waitFor } from "@testing-library/react";

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
    user: { id: "user_1", fullName: "Test User" },
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
  useBilling: () => ({ refresh: vi.fn(), account: null, isSubscribed: true }),
}));

const syncUserMock = vi.fn().mockResolvedValue(undefined);
vi.mock("@/lib/api", () => ({
  useApi: () => ({ syncUser: syncUserMock }),
}));
vi.mock("@/components/support/SupportProvider", () => ({
  useSupport: () => ({ openSupport: vi.fn() }),
}));

vi.mock("@/components/chat/ProvisioningStepper", () => ({
  ProvisioningStepper: ({ children }: { children?: React.ReactNode }) => children ?? null,
}));
vi.mock("@/components/chat/GallerySection", () => ({ GallerySection: () => null }));
vi.mock("@/components/chat/HealthIndicator", () => ({ HealthIndicator: () => null }));
vi.mock("@/components/chat/TrialBanner", () => ({ TrialBanner: () => null }));
vi.mock("@/components/chat/LaunchJoinBanner", () => ({ LaunchJoinBanner: () => null }));
vi.mock("@/components/chat/LaunchJoinModal", () => ({ LaunchJoinModal: () => null }));
vi.mock("@/components/chat/OutOfCreditsBanner", () => ({ OutOfCreditsBanner: () => null }));
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

// Ruling M6-R53: which runtime RPC the shell itself dials.
const rpcMethods: (string | null)[] = [];
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    rpcMethods.push(method);
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
  useRuntimeRpcMutation: () => vi.fn(),
}));

import { ChatLayout } from "../ChatLayout";

function renderLayout() {
  return render(
    <ChatLayout>
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
    let first!: ReturnType<typeof renderLayout>;
    await act(async () => { first = renderLayout(); });
    await waitFor(() => expect(myListingsMock).toHaveBeenCalledTimes(1));
    first.unmount();

    await act(async () => { renderLayout(); });
    // Allow any (incorrect) refetch to land before asserting.
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    expect(myListingsMock).toHaveBeenCalledTimes(1);
  });

  it("fires users/sync once per session across remounts", async () => {
    let first!: ReturnType<typeof renderLayout>;
    await act(async () => { first = renderLayout(); });
    await waitFor(() => expect(syncUserMock).toHaveBeenCalledTimes(1));
    first.unmount();

    await act(async () => { renderLayout(); });
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    expect(syncUserMock).toHaveBeenCalledTimes(1);
  });
});

/**
 * Ruling M6-R53. `models.list` on a BYO-key / ChatGPT-OAuth workspace is served by the bridge,
 * so dialling it from the shell cold-starts a microVM (~55 s) on every `/chat` load — for a
 * sidebar label. The roster row carries `modelName` instead; only the picker surfaces dial
 * the list.
 */
describe("ChatLayout — the shell dials no runtime RPC at all", () => {
  beforeEach(() => {
    rpcMethods.length = 0;
    myListingsMock.mockReset().mockResolvedValue({ items: [] });
    syncUserMock.mockReset().mockResolvedValue(undefined);
    sessionStorage.clear();
  });

  it("dials nothing for model labels", async () => {
    await act(async () => { renderLayout(); });
    expect(rpcMethods).not.toContain("models.list");
    expect(rpcMethods).not.toContain("config.get");
  });
});
