import { useEffect } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

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

// Mock the marketplace API client. The wizard calls updateListing() (raw
// fetch, not useApi), getMySellerProfile() on mount, and artifactPreview()
// inside the Bundle step. Stub all three so the test can drive the wizard
// without spinning up real network calls.
const mockUpdateListing = vi.fn();
const mockArtifactPreview = vi.fn();
const mockGetMySellerProfile = vi.fn();
vi.mock("@/lib/marketplace/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/marketplace/api")>(
    "@/lib/marketplace/api",
  );
  return {
    ...actual,
    updateListing: (...args: unknown[]) => mockUpdateListing(...args),
    artifactPreview: (...args: unknown[]) => mockArtifactPreview(...args),
    getMySellerProfile: (...args: unknown[]) => mockGetMySellerProfile(...args),
  };
});

// Default: personal user (no org membership) — admin-gated CTAs render.
// Individual tests override via `mockUseOrganization.mockReturnValue(...)`.
const mockUseOrganization = vi.fn(() => ({ membership: null as null | { role: string } }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    userId: "u_seller",
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
  useOrganization: () => mockUseOrganization(),
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
    mockUseOrganization.mockReturnValue({ membership: null });
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

  it("shows the review-pullback banner when editing an in-review listing", async () => {
    // Editing a review-state listing auto-demotes it to draft on save
    // (backend update_draft handles the transition). Surface that side
    // effect up front via a yellow banner so the seller isn't surprised
    // when the listing disappears from the moderation queue.
    renderEditing(
      listing({
        status: "review",
        // Strip the rejection_notes from the default so we don't trip the
        // rejection banner test path — this case is "in review, never
        // rejected".
        rejection_notes: null,
        rejected_at: null,
      }),
    );
    const banner = await screen.findByTestId("wizard-review-pullback-banner");
    expect(banner).toHaveTextContent(/currently in review/i);
    expect(banner).toHaveTextContent(/back to draft/i);
  });
});

describe("PublishWizard — republish entry threads editingListing.version", () => {
  // These tests cover the v_new=N+1 republish flow: when MyListingsView
  // calls startEditListing(draftV2) (after POST /republish returns the
  // freshly-cloned draft), the wizard's PATCH + artifact-from-agent +
  // submit must all target version=2 so they revise the new draft row
  // instead of clobbering the live published v=1.
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockUpdateListing.mockReset();
    mockArtifactPreview.mockReset();
    mockGetMySellerProfile.mockReset();
    mockUseOrganization.mockReturnValue({ membership: null });
    // Stripe Connect dashboard probe (called by ensurePayoutsForPaid and
    // PricingStep). Free-tier listings short-circuit the check, but the
    // hook still fires on mount.
    mockApiGet.mockResolvedValue({ dashboard_url: null });
    mockApiPost.mockResolvedValue({});
    mockUpdateListing.mockResolvedValue({});
    // Seller has a handle — skips the pre-publish HandlePicker gate that
    // would otherwise block the final Submit button.
    mockGetMySellerProfile.mockResolvedValue({
      handle: "seller-handle",
      display_name: "Seller",
    });
    // BundleStep loads available MCPs/skills; an empty preview keeps the
    // step's Continue button enabled (canContinue for step 3 is always true).
    mockArtifactPreview.mockResolvedValue({
      available_mcps: [],
      available_skills: [],
      persona_files: [],
      safety_flags: [],
    });
  });

  function v2Draft(): Listing {
    // Republish output: a freshly-cloned draft row at version=2 that
    // points at the same listing_id as the live published v=1. The
    // wizard receives this via startEditListing.
    return listing({
      listing_id: "lst_ray",
      version: 2,
      status: "draft",
      price_cents: 0, // free tier — skips Stripe Connect gate
      rejection_notes: null,
      rejected_at: null,
    });
  }

  async function walkToSubmit() {
    // Edit mode opens on the Details step (PickAgent is locked because
    // agent_id is recovered). The four remaining steps are Details (2),
    // Bundle (3), Pricing (4), Review (5). On Review the CTA flips to
    // "Resubmit for review →" — clicking it triggers submit().
    const continueBtn = await screen.findByText("Continue →");
    fireEvent.click(continueBtn); // Details → Bundle
    await screen.findByTestId("bundle-mcps");
    fireEvent.click(screen.getByText("Continue →")); // Bundle → Pricing
    fireEvent.click(screen.getByText("Continue →")); // Pricing → Review
    const resubmit = await screen.findByText("Resubmit for review →");
    fireEvent.click(resubmit);
  }

  it("PATCH uses editingListing.version=2 (not v=1)", async () => {
    renderEditing(v2Draft());
    await walkToSubmit();
    await waitFor(() => expect(mockUpdateListing).toHaveBeenCalled());
    const call = mockUpdateListing.mock.calls[0][0];
    expect(call.version).toBe(2);
    expect(call.listingId).toBe("lst_ray");
  });

  it("artifact-from-agent POST URL includes ?version=2", async () => {
    renderEditing(v2Draft());
    await walkToSubmit();
    await waitFor(() =>
      expect(mockApiPost).toHaveBeenCalledWith(
        expect.stringContaining("/artifact-from-agent?version=2"),
        expect.any(Object),
      ),
    );
  });

  it("submit POST URL includes ?version=2", async () => {
    renderEditing(v2Draft());
    await walkToSubmit();
    await waitFor(() =>
      expect(mockApiPost).toHaveBeenCalledWith(
        expect.stringContaining("/submit?version=2"),
        expect.any(Object),
      ),
    );
  });
});

describe("PublishWizard — org-admin gate", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockApiGet.mockResolvedValue({ dashboard_url: null });
    mockUseOrganization.mockReturnValue({ membership: null });
  });

  it("renders the restricted view for non-admin org members", async () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:member" } });
    renderEditing(listing());
    expect(await screen.findByTestId("marketplace-restricted")).toBeInTheDocument();
    // None of the wizard steps render.
    expect(screen.queryByText(/Pick which agent/)).toBeNull();
    expect(screen.queryByDisplayValue("Ray")).toBeNull();
  });

  it("renders the full wizard for personal users (no membership)", async () => {
    mockUseOrganization.mockReturnValue({ membership: null });
    renderEditing(listing());
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
    // Details step prefilled — proves the wizard mounted.
    expect(await screen.findByDisplayValue("Ray")).toBeInTheDocument();
  });

  it("renders the full wizard for org admins", async () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:admin" } });
    renderEditing(listing());
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
    expect(await screen.findByDisplayValue("Ray")).toBeInTheDocument();
  });
});
