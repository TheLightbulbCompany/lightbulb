import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SWRConfig } from "swr";
import type { ReactNode } from "react";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post }) }));

const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

// Stripe's embedded component loads a remote script; stand it in with a marker
// that exposes onExit so the card's refresh can be driven.
vi.mock("@stripe/connect-js", () => ({ loadConnectAndInitialize: vi.fn(() => ({})) }));
vi.mock("@stripe/react-connect-js", () => ({
  ConnectComponentsProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
  ConnectAccountOnboarding: ({ onExit }: { onExit: () => void }) => (
    <button type="button" onClick={onExit}>
      exit onboarding
    </button>
  ),
}));

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

const BOUND_BY_KEY = { ...BOUND, source: "api_key" };

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

  it("offers the API-key door plus both Stripe doors when nothing is bound", async () => {
    get.mockResolvedValue(UNBOUND);
    renderSection(true);
    expect(await screen.findByText("Add API key")).toBeInTheDocument();
    expect(screen.getByText("Set up Stripe")).toBeInTheDocument();
    expect(screen.getByText("Connect your Stripe")).toBeInTheDocument();
  });

  it("shows Finish Stripe setup when the projects capability is not active yet", async () => {
    // Charges are on, so the plain "Finish onboarding" would hide — but the
    // account cannot provision until Stripe activates `projects`.
    get.mockResolvedValue({ ...BOUND, projects_ready: false });
    renderSection(true);
    expect(await screen.findByText("Finish Stripe setup")).toBeInTheDocument();
    expect(screen.queryByText("Finish onboarding")).not.toBeInTheDocument();
  });

  it("asks a connected owner for a services account and onboards it in the page", async () => {
    get.mockResolvedValue({ ...BOUND, source: "connected", projects_ready: false, publishable_key: "pk_test_x" });
    post.mockResolvedValue({ account_id: "acct_svc" });
    renderSection(true);
    expect(await screen.findByText(/needs a services account/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Add a services account"));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/services-account", {}));
    expect(await screen.findByTestId("stripe-embedded-onboarding")).toBeInTheDocument();
  });

  it("asks a pasted-key owner for a services account too", async () => {
    get.mockResolvedValue({ ...BOUND_BY_KEY, projects_ready: false, publishable_key: "pk_test_x" });
    renderSection(true);
    expect(await screen.findByText("Add a services account")).toBeInTheDocument();
    expect(screen.queryByText("Finish Stripe setup")).not.toBeInTheDocument();
  });

  it("offers to finish the services account once it exists", async () => {
    get.mockResolvedValue({
      ...BOUND,
      source: "connected",
      projects_ready: false,
      services_account_id: "acct_svc",
      publishable_key: "pk_test_x",
    });
    renderSection(true);
    fireEvent.click(await screen.findByText("Finish services account setup"));
    expect(await screen.findByTestId("stripe-embedded-onboarding")).toBeInTheDocument();
    expect(screen.queryByText("Add a services account")).toBeNull();
  });

  it("Set up Stripe creates the account, then onboards it in the page and refreshes on exit", async () => {
    get.mockResolvedValueOnce({ ...UNBOUND, publishable_key: "pk_test_x" }).mockResolvedValue({
      ...BOUND,
      charges_enabled: false,
      details_submitted: false,
      publishable_key: "pk_test_x",
    });
    post.mockResolvedValue({ account_id: "acct_new", onboarding_url: "https://x" });
    const open = vi.spyOn(window, "open");
    renderSection(true);
    fireEvent.click(await screen.findByText("Set up Stripe"));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/create", {}));
    const exit = await screen.findByText("exit onboarding");
    expect(open).not.toHaveBeenCalled();
    const calls = get.mock.calls.length;
    fireEvent.click(exit);
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(calls));
    expect(screen.queryByTestId("stripe-embedded-onboarding")).toBeNull();
  });

  it("Finish onboarding opens the embedded component when a publishable key is set", async () => {
    get.mockResolvedValue({ ...BOUND, charges_enabled: false, publishable_key: "pk_test_x" });
    renderSection(true);
    fireEvent.click(await screen.findByText("Finish onboarding"));
    expect(await screen.findByTestId("stripe-embedded-onboarding")).toBeInTheDocument();
  });

  it("themes Stripe's embedded component with the page's resolved colours", async () => {
    const { loadConnectAndInitialize } = await import("@stripe/connect-js");
    document.documentElement.style.setProperty("--fg", "#ECECEE");
    get.mockResolvedValue({ ...BOUND, charges_enabled: false, publishable_key: "pk_test_x" });
    renderSection(true);
    fireEvent.click(await screen.findByText("Finish onboarding"));
    await screen.findByTestId("stripe-embedded-onboarding");
    const opts = vi.mocked(loadConnectAndInitialize).mock.lastCall?.[0];
    expect(opts?.appearance?.variables?.colorText).toBe("#ECECEE");
    document.documentElement.style.removeProperty("--fg");
  });

  it("hides Finish Stripe setup once projects is ready", async () => {
    get.mockResolvedValue({ ...BOUND, projects_ready: true });
    renderSection(true);
    await screen.findByText("Open Stripe Dashboard");
    expect(screen.queryByText("Finish Stripe setup")).not.toBeInTheDocument();
  });

  it("without embedded onboarding, Add a services account creates it, then opens its hosted link", async () => {
    get.mockResolvedValue({ ...BOUND, source: "connected", projects_ready: false, publishable_key: null });
    post.mockImplementation(async (path: string) =>
      path === "/company/stripe/onboarding-link"
        ? { onboarding_url: "https://connect.stripe.com/setup/svc" }
        : { account_id: "acct_svc" },
    );
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    renderSection(true);

    fireEvent.click(await screen.findByText("Add a services account"));

    expect(window.open).toHaveBeenCalledWith("", "_blank");
    await waitFor(() => expect(tab.location.href).toBe("https://connect.stripe.com/setup/svc"));
    expect(post.mock.calls.map((c) => c[0])).toEqual([
      "/company/stripe/services-account",
      "/company/stripe/onboarding-link",
    ]);
    expect(post).toHaveBeenLastCalledWith("/company/stripe/onboarding-link", { target: "services" });
    expect(screen.queryByTestId("stripe-embedded-onboarding")).toBeNull();
  });

  it("without embedded onboarding, Finish services account setup opens its hosted link", async () => {
    get.mockResolvedValue({
      ...BOUND,
      source: "api_key",
      projects_ready: false,
      services_account_id: "acct_svc",
      publishable_key: null,
    });
    post.mockResolvedValue({ onboarding_url: "https://connect.stripe.com/setup/svc" });
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    renderSection(true);

    fireEvent.click(await screen.findByText("Finish services account setup"));

    await waitFor(() => expect(tab.location.href).toBe("https://connect.stripe.com/setup/svc"));
    expect(post).toHaveBeenCalledWith("/company/stripe/onboarding-link", { target: "services" });
  });

  it("labels a binding made with a test-mode key so sandbox figures never read as revenue", async () => {
    get.mockResolvedValue({ ...BOUND_BY_KEY, livemode: false });
    renderSection(true);
    expect(await screen.findByText("Your API key · Test mode")).toBeInTheDocument();
  });

  it("pastes a key: posts it, clears the field, and refreshes status", async () => {
    get.mockResolvedValueOnce(UNBOUND).mockResolvedValue(BOUND_BY_KEY);
    post.mockResolvedValue({ account_id: "acct_x", livemode: true });
    renderSection(true);
    fireEvent.click(await screen.findByText("Add API key"));
    const input = screen.getByPlaceholderText("rk_live_…") as HTMLInputElement;
    // The permission list is right there so nobody guesses at ~80 boxes.
    expect(screen.getByText(/Charges, Customers, Products, Prices, Balance, Account/)).toBeInTheDocument();
    // The paste field is a password input: the key never renders in the DOM.
    expect(input.type).toBe("password");
    fireEvent.change(input, { target: { value: "  rk_live_abc  " } });
    fireEvent.click(screen.getByText("Connect"));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/api-key", { api_key: "rk_live_abc" }));
    expect(await screen.findByText("Your API key")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("rk_live_…")).toBeNull();
  });

  it("Connect is disabled until something is typed, and Cancel puts the door back", async () => {
    get.mockResolvedValue(UNBOUND);
    renderSection(true);
    fireEvent.click(await screen.findByText("Add API key"));
    expect(screen.getByText("Connect")).toBeDisabled();
    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.getByText("Add API key")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("a rejected key surfaces the API error and keeps the field open", async () => {
    get.mockResolvedValue(UNBOUND);
    post.mockRejectedValue(new Error("invalid_api_key"));
    renderSection(true);
    fireEvent.click(await screen.findByText("Add API key"));
    fireEvent.change(screen.getByPlaceholderText("rk_live_…"), { target: { value: "rk_live_bad" } });
    fireEvent.click(screen.getByText("Connect"));
    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    expect(screen.getByPlaceholderText("rk_live_…")).toBeInTheDocument();
  });

  it("an api_key row can Disconnect even when Connect is unconfigured", async () => {
    // Nothing at Stripe to revoke, so the client id is irrelevant to it.
    get.mockResolvedValue({ ...BOUND_BY_KEY, connect_configured: false });
    renderSection(true);
    expect(await screen.findByText("Your API key")).toBeInTheDocument();
    expect(screen.getByText("Disconnect")).not.toBeDisabled();
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

  it("hides BOTH Connect doors when Connect is unconfigured, and disables Disconnect for a Connect row", async () => {
    // "Set up Stripe" used to survive an unconfigured client id. It no longer
    // does: it mints a Connect account too, and without the client id that
    // account can never be disconnected — the trap `disconnect` documents.
    // The API-key door needs no client id and is unaffected.
    get.mockResolvedValue({ ...UNBOUND, connect_configured: false });
    const { unmount } = renderSection(true);
    expect(await screen.findByText("Add API key")).toBeInTheDocument();
    expect(screen.queryByText("Set up Stripe")).toBeNull();
    expect(screen.queryByText("Connect your Stripe")).toBeNull();
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
    // The grant heading "Company account" is the unambiguous marker that
    // loading has resolved — the desc paragraph and the non-admin message
    // both contain the substring "Stripe", so a plain findByText(/Stripe/)
    // here throws on multiple matches.
    await screen.findByRole("heading", { name: "Company account" });
    expect(screen.queryByText("Set up Stripe")).toBeNull();
  });
});
