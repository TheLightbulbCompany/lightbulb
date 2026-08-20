import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockSearch = vi.fn();
const mockDeployCompany = vi.fn();
vi.mock("@/lib/marketplace/api", () => ({
  searchListings: (opts: unknown) => mockSearch(opts),
  deployCompany: (opts: unknown) => mockDeployCompany(opts),
}));
vi.mock("@/hooks/useCatalog", () => ({
  useCatalog: () => ({ installedSlugs: new Set(["pulse"]), deploy: vi.fn() }),
}));

// useHire (called from ListingCard) reaches into useAgents -> useGateway. The
// test renders ListingCards without a GatewayProvider, so we mock useAgents
// here to avoid the throw. Same root cause as PR #567 P1: anything inside the
// drawer that uses useHire needs gateway context (real or mocked).
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [], refresh: vi.fn().mockResolvedValue(undefined) }),
}));

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    // `installedSlugs` is the optimistic just-hired overlay BrowseView unions
    // with the catalog's set — omit it and every ListingCard render throws on
    // `state.installedSlugs.has(...)`.
    state: { freshlyInstalledAgentId: null, installedSlugs: new Set<string>(), toast: null },
    open: vi.fn(),
    close: vi.fn(),
    setView: vi.fn(),
    startEditListing: vi.fn(),
    openListingDetail: vi.fn(),
    showInstallToast: vi.fn(),
    showErrorToast: vi.fn(),
    dismissToast: vi.fn(),
    setFreshlyInstalled: vi.fn(),
    markInstalled: vi.fn(),
  }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("tok_test") }),
  useUser: () => ({ user: { primaryEmailAddress: { emailAddress: "buyer@example.com" } } }),
  // ListingCard now uses the admin gate. null membership = personal user → gate open.
  useOrganization: () => ({ membership: null }),
}));

import { BrowseView } from "@/components/marketplace/drawer/BrowseView";

const SAMPLE = [
  {
    listing_id: "l1", slug: "atlas", name: "Atlas", description_md: "Engineering helper",
    seller_id: "u_curator", price_cents: 0, status: "published", format: "openclaw",
    tags: ["engineering"], version: 1, created_at: "2026-05-01", published_at: "2026-05-01",
  },
  {
    listing_id: "l2", slug: "pulse", name: "Pulse", description_md: "Sales digest",
    seller_id: "u_curator", price_cents: 1200, status: "published", format: "openclaw",
    tags: ["sales"], version: 1, created_at: "2026-05-01", published_at: "2026-05-01",
  },
];

describe("BrowseView", () => {
  beforeEach(() => mockSearch.mockReset());

  it("renders cards from searchListings response", async () => {
    mockSearch.mockResolvedValueOnce({ items: SAMPLE });
    render(<BrowseView />);
    await waitFor(() => expect(screen.getByText("Atlas")).toBeInTheDocument());
    expect(screen.getByText("Pulse")).toBeInTheDocument();
  });

  it("already-installed listings show 'Hired ✓' instead of Hire", async () => {
    mockSearch.mockResolvedValueOnce({ items: SAMPLE });
    render(<BrowseView />);
    await waitFor(() => expect(screen.getByText("Pulse")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /hired ✓/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^hire$/i })).toBeVisible();
  });

  it("category chip click refetches with the category param", async () => {
    mockSearch.mockResolvedValue({ items: [] });
    const user = userEvent.setup();
    render(<BrowseView />);
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: /sales/i }));
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(2));
    expect(mockSearch).toHaveBeenLastCalledWith(
      expect.objectContaining({ category: "sales" }),
    );
  });

  it("renders six category chips per spec", async () => {
    mockSearch.mockResolvedValueOnce({ items: [] });
    render(<BrowseView />);
    await waitFor(() => expect(mockSearch).toHaveBeenCalled());
    ["all", "sales", "engineering", "operations", "research", "finance"].forEach((c) => {
      expect(screen.getByRole("button", { name: new RegExp(`^${c}$`, "i") })).toBeVisible();
    });
  });

  it("Companies scope refetches with format=company and renders company cards", async () => {
    const company = {
      listing_id: "lc1", slug: "gstack", name: "GStack", description_md: "A whole company",
      seller_id: "__isol8__", price_cents: 0, status: "published", format: "company",
      tags: [], version: 1, created_at: "2026-07-01", published_at: "2026-07-01",
      manifest_json: {
        company: {
          slug: "gstack", name: "GStack", description: "Engineering org in a box",
          agent_count: 5, team_count: 1, skill_count: 12, agent_slugs: ["ceo"],
          env_requirements: [], min_plan: "standard",
        },
      },
    };
    mockSearch.mockResolvedValueOnce({ items: SAMPLE }).mockResolvedValueOnce({ items: [company] });
    const user = userEvent.setup();
    render(<BrowseView />);
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("tab", { name: /companies/i }));

    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(2));
    expect(mockSearch).toHaveBeenLastCalledWith(expect.objectContaining({ format: "company" }));
    await waitFor(() => expect(screen.getByText("GStack")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /deploy company/i })).toBeEnabled();
    // Category chips are agent-only — hidden in the companies scope.
    expect(screen.queryByRole("button", { name: /^sales$/i })).toBeNull();
  });

  it("scope switch clears the grid immediately — no stale cross-format cards", async () => {
    // The refetch is debounced 300ms; until it lands the grid must show
    // Loading, never the OLD scope's rows through the NEW scope's card
    // (agent rows rendered a phantom Hire button as company rows, and vice
    // versa an all-null blank grid).
    mockSearch.mockResolvedValueOnce({ items: SAMPLE }).mockReturnValueOnce(new Promise(() => {}));
    const user = userEvent.setup();
    render(<BrowseView />);
    await waitFor(() => expect(screen.getByText("Atlas")).toBeInTheDocument());

    await user.click(screen.getByRole("tab", { name: /companies/i }));

    expect(screen.queryByText("Atlas")).toBeNull();
    expect(screen.queryByRole("button", { name: /^hire$/i })).toBeNull();
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });
});
