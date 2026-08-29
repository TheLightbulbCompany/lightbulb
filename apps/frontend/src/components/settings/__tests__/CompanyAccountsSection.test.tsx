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

import { CompanyAccountsSection, dashboardHref } from "@/components/settings/CompanyAccountsSection";

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

  it("ignores an off-host dashboard url and falls back to the provider home", async () => {
    // `dashboard_url` is agent-supplied; an attacker-chosen host must never
    // become the owner's click target.
    get.mockResolvedValue({
      ...ONE,
      resources: [{ ...ONE.resources[0], dashboard_url: "https://evil.example.com/pwn" }],
    });
    renderSection(true);
    expect(await screen.findByText("Open dashboard")).toHaveAttribute("href", "https://posthog.com");
  });

  it("renders no link at all for a provider it has no home page for", async () => {
    get.mockResolvedValue({
      ...ONE,
      resources: [{ ...ONE.resources[0], provider: "acme", dashboard_url: null }],
    });
    renderSection(true);
    await screen.findByText("acme");
    expect(screen.queryByText("Open dashboard")).toBeNull();
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

describe("dashboardHref", () => {
  it("keeps a url on the provider's own host or a subdomain of it", () => {
    expect(dashboardHref("posthog", "https://posthog.com/project/1")).toBe("https://posthog.com/project/1");
    expect(dashboardHref("posthog", "https://us.posthog.com/project/1")).toBe("https://us.posthog.com/project/1");
  });

  it("rejects anything else and falls back, or gives no link", () => {
    // Off-host, look-alike host, non-https and unparseable all fall back.
    for (const bad of [
      "https://evil.example.com",
      "https://notposthog.com",
      "http://posthog.com",
      "javascript:alert(1)",
      "not a url",
    ]) {
      expect(dashboardHref("posthog", bad)).toBe("https://posthog.com");
    }
    // No known home page => no link, never a guessed https://{provider}.com.
    expect(dashboardHref("acme", null)).toBeNull();
    expect(dashboardHref("acme", "https://acme.com")).toBeNull();
    // Prototype keys are not home pages.
    expect(dashboardHref("constructor", null)).toBeNull();
  });
});
