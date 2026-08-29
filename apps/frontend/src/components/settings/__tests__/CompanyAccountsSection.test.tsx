import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SWRConfig } from "swr";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post }) }));

// `@/lib/notify` imports ApiError from the mocked `@/lib/api`; stub it the
// same way CompanyStripeSection.test.tsx does.
const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

import { CompanyAccountsSection } from "@/components/settings/CompanyAccountsSection";

// Fresh SWR cache per render (same convention as CompanyStripeSection.test.tsx).
function renderSection(canManage: boolean) {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <CompanyAccountsSection canManage={canManage} />
    </SWRConfig>,
  );
}

const ONE = {
  resources: [{
    resource_id: "res_1", provider: "posthog", service: "analytics", status: "active",
    resource_name: "Northwind analytics", env_keys: ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
    dashboard_url: "https://us.posthog.com/project/1", level: "company",
  }],
  stripe_cli: "paired",
};

describe("CompanyAccountsSection", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    notifyApiError.mockReset();
    notifyError.mockReset();
  });

  it("says the CLI is not connected when nothing is provisioned", async () => {
    get.mockResolvedValue({ resources: [], stripe_cli: "unpaired" });
    renderSection(true);
    expect(await screen.findByText("Stripe CLI: not connected")).toBeInTheDocument();
    // No owner-side pairing button: pairing starts from the agent's card.
    expect(screen.queryByText("Pair Stripe CLI")).toBeNull();
    expect(screen.getByText(/Ask your agents/)).toBeInTheDocument();
  });

  it("renders one row per resource with its level and a dashboard link", async () => {
    get.mockResolvedValue(ONE);
    renderSection(true);
    expect(await screen.findByText("posthog")).toBeInTheDocument();
    expect(screen.getByText("analytics")).toBeInTheDocument();
    expect(screen.getByText("Northwind analytics")).toBeInTheDocument();
    expect(screen.getByText("Company-level")).toBeInTheDocument();
    expect(screen.getByText("Open dashboard")).toHaveAttribute("href", "https://us.posthog.com/project/1");
    expect(screen.getByText("Stripe CLI: connected")).toBeInTheDocument();
  });

  it("falls back to the provider homepage when there is no dashboard url", async () => {
    get.mockResolvedValue({ ...ONE, resources: [{ ...ONE.resources[0], dashboard_url: null }] });
    renderSection(true);
    expect(await screen.findByText("Open dashboard")).toHaveAttribute("href", "https://posthog.com");
  });

  it("revoke deletes our copy and says the owner keeps the account", async () => {
    get.mockResolvedValue(ONE);
    post.mockResolvedValue({ revoked: true });
    renderSection(true);
    fireEvent.click(await screen.findByText("Revoke"));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/resources/posthog/res_1/revoke", {}),
    );
    expect(await screen.findByText(/You keep the account/)).toBeInTheDocument();
  });

  it("hides revoke for non-admins", async () => {
    get.mockResolvedValue(ONE);
    renderSection(false);
    await screen.findByText("posthog");
    expect(screen.queryByText("Revoke")).toBeNull();
  });
});
