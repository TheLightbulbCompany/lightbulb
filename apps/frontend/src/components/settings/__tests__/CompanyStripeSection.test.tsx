import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SWRConfig } from "swr";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post }) }));

const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

import { CompanyStripeSection } from "@/components/settings/CompanyStripeSection";

// The two-phase tab: opened EMPTY and synchronously in the click handler,
// then pointed at the URL the API mints. `location.href` is where the tab
// actually ends up, so that (not the window.open argument) is the assertion.
function fakeTab() {
  return { opener: undefined, location: { href: "" }, close: vi.fn() };
}

const UNBOUND = {
  account_id: null,
  source: null,
  details_submitted: false,
  charges_enabled: false,
  payouts_enabled: false,
  dashboard_url: null,
  connect_configured: true,
};

const BOUND = {
  account_id: "acct_x",
  source: "created",
  details_submitted: true,
  charges_enabled: true,
  payouts_enabled: false,
  dashboard_url: "https://dashboard.stripe.com/",
  connect_configured: true,
};

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
    notifyApiError.mockReset();
    notifyError.mockReset();
  });

  it("offers both doors when nothing is bound", async () => {
    get.mockResolvedValue(UNBOUND);
    renderSection(true);
    expect(await screen.findByText("Set up Stripe")).toBeInTheDocument();
    expect(screen.getByText("Connect existing Stripe")).toBeInTheDocument();
  });

  it("opens the tab synchronously on click, then points it at the onboarding link", async () => {
    get.mockResolvedValue(UNBOUND);
    let resolvePost: (v: unknown) => void = () => {};
    post.mockImplementation(() => new Promise((r) => { resolvePost = r; }));
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    renderSection(true);

    fireEvent.click(await screen.findByText("Set up Stripe"));

    // Synchronously, BEFORE the API promise settles — a window.open after an
    // await is blocked by Safari.
    expect(window.open).toHaveBeenCalledWith("", "_blank");
    expect(tab.location.href).toBe("");

    resolvePost({ account_id: "acct_new", onboarding_url: "https://connect.stripe.com/setup/x" });
    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/create", {}));
    await waitFor(() => expect(tab.location.href).toBe("https://connect.stripe.com/setup/x"));
  });

  it("reports a blocked popup and never calls the API", async () => {
    get.mockResolvedValue(UNBOUND);
    vi.spyOn(window, "open").mockImplementation(() => null);
    renderSection(true);

    fireEvent.click(await screen.findByText("Set up Stripe"));

    await waitFor(() => expect(notifyError).toHaveBeenCalled());
    expect(post).not.toHaveBeenCalled();
  });

  it("closes the pending tab when the API call fails", async () => {
    get.mockResolvedValue(UNBOUND);
    post.mockRejectedValue(new Error("502"));
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    renderSection(true);

    fireEvent.click(await screen.findByText("Set up Stripe"));

    await waitFor(() => expect(tab.close).toHaveBeenCalled());
    expect(notifyApiError).toHaveBeenCalled();
    expect(tab.location.href).toBe("");
  });

  it("shows status and disconnect when bound", async () => {
    get.mockResolvedValue(BOUND);
    renderSection(true);
    expect(await screen.findByText(/Charges enabled/)).toBeInTheDocument();
    expect(screen.getByText("Disconnect")).toBeInTheDocument();
  });

  it("hides Connect existing and disables Disconnect when Connect is unconfigured", async () => {
    get.mockResolvedValue({ ...UNBOUND, connect_configured: false });
    const { unmount } = renderSection(true);
    expect(await screen.findByText("Set up Stripe")).toBeInTheDocument();
    expect(screen.queryByText("Connect existing Stripe")).toBeNull();
    unmount();

    get.mockResolvedValue({ ...BOUND, connect_configured: false });
    renderSection(true);
    const disconnect = await screen.findByText("Disconnect");
    expect(disconnect).toBeDisabled();
    expect(disconnect).toHaveAttribute("title", "Not available until Stripe Connect is configured");
  });

  it("hides actions for non-admins", async () => {
    get.mockResolvedValue(UNBOUND);
    renderSection(false);
    // The card title "Stripe" is the unambiguous marker that loading has
    // resolved — the desc paragraph and the non-admin message both also
    // contain the substring "Stripe", so a plain findByText(/Stripe/) here
    // throws on multiple matches.
    await screen.findByRole("heading", { name: "Stripe" });
    expect(screen.queryByText("Set up Stripe")).toBeNull();
  });
});
