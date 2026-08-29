import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SWRConfig } from "swr";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post }) }));

import { CompanyStripeSection } from "@/components/settings/CompanyStripeSection";

// SWR has a global cache that persists across renders/tests — wrap each
// render in a fresh SWRConfig with `provider: () => new Map()` to isolate
// (same convention as AgentEmailSection.test.tsx).
function renderSection(canManage: boolean) {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <CompanyStripeSection canManage={canManage} />
    </SWRConfig>,
  );
}

describe("CompanyStripeSection", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    // A non-null return represents a successful (non-popup-blocked) open —
    // openInNewTab() treats only `null` as blocked, and a blocked open now
    // routes through notifyApiError(), which needs a real "@/lib/api" mock
    // shape (ApiError) this file doesn't provide. Popup-blocked handling
    // itself is covered by ApprovalRow.connectStripe.test.tsx.
    vi.spyOn(window, "open").mockImplementation(() => window);
  });

  it("offers both doors when nothing is bound", async () => {
    get.mockResolvedValue({ account_id: null, source: null, details_submitted: false, charges_enabled: false, payouts_enabled: false, dashboard_url: null });
    renderSection(true);
    expect(await screen.findByText("Set up Stripe")).toBeInTheDocument();
    expect(screen.getByText("Connect existing Stripe")).toBeInTheDocument();
  });

  it("creates and opens the onboarding link", async () => {
    get.mockResolvedValue({ account_id: null, source: null, details_submitted: false, charges_enabled: false, payouts_enabled: false, dashboard_url: null });
    post.mockResolvedValue({ account_id: "acct_new", onboarding_url: "https://connect.stripe.com/setup/x" });
    renderSection(true);
    fireEvent.click(await screen.findByText("Set up Stripe"));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/create", {}));
    expect(window.open).toHaveBeenCalledWith("https://connect.stripe.com/setup/x", "_blank", "noopener");
  });

  it("shows status and disconnect when bound", async () => {
    get.mockResolvedValue({ account_id: "acct_x", source: "created", details_submitted: true, charges_enabled: true, payouts_enabled: false, dashboard_url: "https://dashboard.stripe.com/" });
    renderSection(true);
    expect(await screen.findByText(/Charges enabled/)).toBeInTheDocument();
    expect(screen.getByText("Disconnect")).toBeInTheDocument();
  });

  it("hides actions for non-admins", async () => {
    get.mockResolvedValue({ account_id: null, source: null, details_submitted: false, charges_enabled: false, payouts_enabled: false, dashboard_url: null });
    renderSection(false);
    // The card title "Stripe" is the unambiguous marker that loading has
    // resolved — the desc paragraph and the non-admin message both also
    // contain the substring "Stripe", so a plain findByText(/Stripe/) here
    // throws on multiple matches.
    await screen.findByRole("heading", { name: "Stripe" });
    expect(screen.queryByText("Set up Stripe")).toBeNull();
  });
});
