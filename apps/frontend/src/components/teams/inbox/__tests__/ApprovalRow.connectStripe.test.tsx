// apps/frontend/src/components/teams/inbox/__tests__/ApprovalRow.connectStripe.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const approve = vi.fn();
const reject = vi.fn();
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

// Failures surface as a toast, not inline row text.
const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

const post = vi.fn();
const get = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post, get }) }));

// The Stripe doors are offered while nothing is bound and Connect is configured.
const stripeStatus = vi.hoisted(() => ({
  loaded: true,
  account_id: null as string | null,
  source: null as string | null,
  connect_configured: true,
  projects_ready: null as boolean | null,
  services_account_id: null as string | null,
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => ({ data: stripeStatus.loaded ? stripeStatus : undefined }),
}));
const { navigateShell } = vi.hoisted(() => ({ navigateShell: vi.fn() }));
vi.mock("@/lib/shellNav", async (orig) => ({ ...(await orig<typeof import("@/lib/shellNav")>()), navigateShell }));

// The two-phase tab: opened EMPTY and synchronously in the click handler,
// then pointed at the URL the API mints (a `window.open` after an `await` is
// blocked by Safari). `location.href` is where the tab actually lands.
function fakeTab() {
  return { opener: undefined, location: { href: "" }, close: vi.fn() };
}

import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

// Paperclip only accepts its own four approval types, so the card is filed as
// `request_board_approval` and `payload.isol8_action` is the discriminator.
const A: Approval = {
  id: "a-stripe",
  type: "request_board_approval",
  payload: { isol8_action: "company_stripe", title: "Set up Northwind on Stripe" },
  status: "pending",
};

beforeEach(() => {
  approve.mockReset().mockResolvedValue(undefined);
  reject.mockReset().mockResolvedValue(undefined);
  notifyApiError.mockReset();
  notifyError.mockReset();
  post.mockReset();
  get.mockReset();
  navigateShell.mockReset();
  Object.assign(stripeStatus, {
    loaded: true,
    account_id: null,
    source: null,
    connect_configured: true,
    projects_ready: null,
    services_account_id: null,
  });
});

describe("ApprovalRow company_stripe", () => {
  it("offers neither door set (and no Approve) until the Stripe status has loaded", () => {
    stripeStatus.loaded = false;
    render(<ApprovalRow approval={A} />);

    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();
    for (const name of ["Set up Stripe", "Connect your Stripe", "Open Accounts", "Approve"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("with Connect unconfigured, offers Open Accounts + Approve instead of the Stripe doors", () => {
    stripeStatus.connect_configured = false;
    render(<ApprovalRow approval={A} />);

    expect(screen.queryByRole("button", { name: "Set up Stripe" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Connect your Stripe" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open Accounts" }));
    expect(navigateShell).toHaveBeenCalledWith("/chat?panel=teams-accounts");
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("shows Connect your Stripe / Set up Stripe (no Approve); Set up Stripe approves, then opens the Stripe card", async () => {
    render(<ApprovalRow approval={A} />);

    expect(screen.getByText("Connect Stripe: Set up Northwind on Stripe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect your Stripe" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-stripe"));
    await waitFor(() => expect(navigateShell).toHaveBeenCalledWith("/chat?panel=teams-accounts"));
    // Onboarding is embedded in the card: no hosted link is minted here.
    expect(post).not.toHaveBeenCalled();
  });

  it("asks a connected owner for a services account when Projects isn't ready", () => {
    Object.assign(stripeStatus, { account_id: "acct_x", source: "connected", projects_ready: false });
    render(<ApprovalRow approval={A} />);

    expect(screen.queryByRole("button", { name: "Connect your Stripe" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add a services account" }));
    expect(navigateShell).toHaveBeenCalledWith("/chat?panel=teams-accounts");
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
  });

  it("a bound company with a services account just opens Accounts", () => {
    Object.assign(stripeStatus, {
      account_id: "acct_x",
      source: "api_key",
      projects_ready: false,
      services_account_id: "acct_svc",
    });
    render(<ApprovalRow approval={A} />);
    expect(screen.getByRole("button", { name: "Open Accounts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add a services account" })).toBeNull();
  });

  it("does not approve — or call the API — when the popup is blocked", async () => {
    get.mockResolvedValue({ authorize_url: "https://connect.stripe.com/oauth/authorize?x" });
    vi.spyOn(window, "open").mockImplementation(() => null);
    render(<ApprovalRow approval={A} />);

    fireEvent.click(screen.getByRole("button", { name: "Connect your Stripe" }));

    await waitFor(() => expect(notifyError).toHaveBeenCalled());
    expect(get).not.toHaveBeenCalled();
    expect(approve).not.toHaveBeenCalled();
  });

  it("closes the pending tab and does not approve when the API call fails", async () => {
    get.mockRejectedValue(new Error("409"));
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    render(<ApprovalRow approval={A} />);

    fireEvent.click(screen.getByRole("button", { name: "Connect your Stripe" }));

    await waitFor(() => expect(tab.close).toHaveBeenCalled());
    expect(notifyApiError).toHaveBeenCalled();
    expect(tab.location.href).toBe("");
    expect(approve).not.toHaveBeenCalled();
  });

  it("desktop (Tauri): does NOT approve when the system-browser open fails", async () => {
    get.mockResolvedValue({ authorize_url: "https://connect.stripe.com/oauth/authorize?x" });
    const invoke = vi.fn().mockRejectedValue("forbidden");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__TAURI__ = { core: { invoke } };
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    try {
      render(<ApprovalRow approval={A} />);
      fireEvent.click(screen.getByRole("button", { name: "Connect your Stripe" }));

      await waitFor(() => expect(invoke).toHaveBeenCalled());
      await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
      expect(approve).not.toHaveBeenCalled();
      expect(openSpy).not.toHaveBeenCalled();
    } finally {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      delete (window as any).__TAURI__;
    }
  });

  it("desktop (Tauri): approves only after the system browser opened", async () => {
    get.mockResolvedValue({ authorize_url: "https://connect.stripe.com/oauth/authorize?x" });
    const invoke = vi.fn().mockResolvedValue(undefined);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__TAURI__ = { core: { invoke } };
    try {
      render(<ApprovalRow approval={A} />);
      fireEvent.click(screen.getByRole("button", { name: "Connect your Stripe" }));

      await waitFor(() =>
        expect(invoke).toHaveBeenCalledWith("plugin:shell|open", {
          path: "https://connect.stripe.com/oauth/authorize?x",
        }),
      );
      await waitFor(() => expect(approve).toHaveBeenCalledWith("a-stripe"));
    } finally {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      delete (window as any).__TAURI__;
    }
  });

  it("Connect your Stripe opens the tab synchronously, then the authorize URL, then approves", async () => {
    get.mockResolvedValue({ authorize_url: "https://connect.stripe.com/oauth/authorize?x" });
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    render(<ApprovalRow approval={A} />);

    fireEvent.click(screen.getByRole("button", { name: "Connect your Stripe" }));

    expect(window.open).toHaveBeenCalledWith("", "_blank");
    await waitFor(() => expect(get).toHaveBeenCalledWith("/company/stripe/connect/start"));
    await waitFor(() => expect(tab.location.href).toBe("https://connect.stripe.com/oauth/authorize?x"));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-stripe"));
  });
});

it("a plain board approval keeps the generic Approve button", () => {
  render(<ApprovalRow approval={{ id: "a-board", type: "request_board_approval", status: "pending" }} />);
  expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Set up Stripe" })).toBeNull();
});
