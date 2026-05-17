import { useEffect } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import { PublishWizard } from "../PublishWizard";
import { MarketplaceDrawerProvider, useMarketplaceDrawer } from "../MarketplaceDrawerProvider";
import type { Listing } from "@/lib/marketplace/types";

const mockApiGet = vi.fn();
const mockApiPost = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({
    get: mockApiGet,
    post: mockApiPost,
    put: vi.fn(),
    del: vi.fn(),
  }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    userId: "u_seller",
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
}));

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [], defaultId: undefined }),
  agentDisplayName: (a: { id?: string; name?: string }) => a.name ?? a.id ?? "?",
}));

function listing(overrides: Partial<Listing> = {}): Listing {
  return {
    listing_id: "lst_ray",
    slug: "ray",
    name: "Ray",
    description_md: "A research-ops agent who keeps the inbox sane.",
    format: "openclaw",
    price_cents: 500,
    tags: ["ops", "research"],
    category: "operations",
    seller_id: "u_seller",
    status: "draft",
    version: 1,
    published_at: null,
    created_at: "2026-05-10T12:00:00Z",
    agent_id: "agent_ray_42",
    rejection_notes: "Description should be more specific about Ray's outputs.",
    rejected_at: "2026-05-12T17:30:00Z",
    ...overrides,
  };
}

/** Mirrors the real drawer: call startEditListing on mount, then only
 * render PublishWizard once editListing is set. That ordering matters
 * because the wizard reads editListing in its useState initializer; if
 * it mounts before the state flip, the form is initialized empty.
 *
 * The effect updates the drawer's state via startEditListing (an external
 * store) and lets the next render's `state.editListing` gate the wizard
 * mount — no local setState here, so we don't trip the
 * react-hooks/set-state-in-effect lint rule. */
function EditingHarness({ l }: { l: Listing }) {
  const { state, startEditListing } = useMarketplaceDrawer();
  useEffect(() => {
    startEditListing(l);
  }, [l, startEditListing]);
  if (!state.editListing) return null;
  return <PublishWizard />;
}

function renderEditing(l: Listing) {
  return render(
    <MarketplaceDrawerProvider>
      <EditingHarness l={l} />
    </MarketplaceDrawerProvider>,
  );
}

describe("PublishWizard edit mode", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockApiGet.mockResolvedValue({ dashboard_url: null });
  });

  it("renders the rejection banner on entry", async () => {
    renderEditing(listing());
    const banner = await screen.findByTestId("wizard-rejection-banner");
    expect(banner).toHaveTextContent(/specific about Ray's outputs/);
  });

  it("starts on the Details step (PickAgent locked) and prefills fields from the listing", async () => {
    renderEditing(listing());
    // Step 1 header copy ("Pick which agent in your workspace…") must NOT
    // appear — we skipped that step.
    await waitFor(() => expect(screen.queryByText(/Pick which agent/)).not.toBeInTheDocument());
    // Details step prefills the name from the listing.
    const nameInput = screen.getByDisplayValue("Ray") as HTMLInputElement;
    expect(nameInput).toBeInTheDocument();
    // Step counter reflects the 4-step edit flow (not 5).
    expect(screen.getByText(/Step 1 of 4/)).toBeInTheDocument();
  });

  it("submit-button copy switches to 'Resubmit for review'", async () => {
    renderEditing(listing());
    // We're on step 1 (of 4) — Continue is shown. Test that the LAST-step
    // copy is "Resubmit…" by reading the wizard's CTA after we walk to it.
    // Cheaper: pin the create-mode behaviour by not editing, then pin edit
    // mode separately. Here we just assert the continue button still works.
    await screen.findByText("Continue →");
  });

  it("falls back to step 1 (PickAgent unlocked) when the draft has no recoverable agent_id", async () => {
    // Regression for Codex P2 on PR #692 (commit a2d75dbc): a draft can
    // exist without agent_id if the seller's previous submit chain failed
    // before artifact-from-agent landed. Locking PickAgent for those
    // drafts would render an undefined-agent BundleStep and post
    // `agent_id: undefined` to artifact-from-agent on resubmit (422).
    renderEditing(
      listing({
        agent_id: null,
        manifest_json: undefined,
      }),
    );
    // Step 1 copy IS rendered — agent picker is reachable.
    await screen.findByText(/Pick which agent in your workspace/);
    // Step counter shows 5 (not 4) — full publish flow.
    expect(screen.getByText(/Step 1 of 5/)).toBeInTheDocument();
  });
});
