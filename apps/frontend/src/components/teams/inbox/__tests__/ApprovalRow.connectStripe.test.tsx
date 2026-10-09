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

// The Connect doors are offered only while the provisioning rail is on.
const stripeStatus = vi.hoisted(() => ({ provisioning_enabled: true, loaded: true }));
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
  stripeStatus.provisioning_enabled = true;
  stripeStatus.loaded = true;
});

describe("ApprovalRow company_stripe", () => {
  it("offers neither door set (and no Approve) until the Stripe status has loaded", () => {
    stripeStatus.loaded = false;
    render(<ApprovalRow approval={A} />);

    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();
    for (const name of ["Set up Stripe", "Connect existing", "Open Accounts", "Approve"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("with provisioning off, offers Open Accounts + Approve instead of the Connect doors", () => {
    stripeStatus.provisioning_enabled = false;
    render(<ApprovalRow approval={A} />);

    expect(screen.queryByRole("button", { name: "Set up Stripe" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Connect existing" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open Accounts" }));
    expect(navigateShell).toHaveBeenCalledWith("/chat?panel=teams-accounts");
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("shows Set up Stripe / Connect existing (no Approve); Set up Stripe posts, navigates the tab, then approves", async () => {
    let resolvePost: (v: unknown) => void = () => {};
    post.mockImplementation(() => new Promise((r) => { resolvePost = r; }));
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    render(<ApprovalRow approval={A} />);

    expect(screen.getByText("Connect Stripe: Set up Northwind on Stripe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set up Stripe" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect existing" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^approve$/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

    // Synchronously, before the API promise settles.
    expect(window.open).toHaveBeenCalledWith("", "_blank");
    expect(tab.location.href).toBe("");
    expect(approve).not.toHaveBeenCalled();

    resolvePost({ onboarding_url: "https://connect.stripe.com/setup/x" });
    await waitFor(() => expect(post).toHaveBeenCalledWith("/company/stripe/create", {}));
    await waitFor(() => expect(tab.location.href).toBe("https://connect.stripe.com/setup/x"));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-stripe"));
  });

  it("does not approve — or call the API — when the popup is blocked", async () => {
    post.mockResolvedValue({ onboarding_url: "https://connect.stripe.com/setup/x" });
    vi.spyOn(window, "open").mockImplementation(() => null);
    render(<ApprovalRow approval={A} />);

    fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

    await waitFor(() => expect(notifyError).toHaveBeenCalled());
    expect(post).not.toHaveBeenCalled();
    expect(approve).not.toHaveBeenCalled();
  });

  it("closes the pending tab and does not approve when the API call fails", async () => {
    post.mockRejectedValue(new Error("409"));
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    render(<ApprovalRow approval={A} />);

    fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

    await waitFor(() => expect(tab.close).toHaveBeenCalled());
    expect(notifyApiError).toHaveBeenCalled();
    expect(tab.location.href).toBe("");
    expect(approve).not.toHaveBeenCalled();
  });

  it("desktop (Tauri): does NOT approve when the system-browser open fails", async () => {
    post.mockResolvedValue({ onboarding_url: "https://connect.stripe.com/setup/x" });
    const invoke = vi.fn().mockRejectedValue("forbidden");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__TAURI__ = { core: { invoke } };
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    try {
      render(<ApprovalRow approval={A} />);
      fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

      await waitFor(() => expect(invoke).toHaveBeenCalled());
      // The failed open surfaces as an error and the hand-off is NOT treated
      // as seen: no approval is recorded for a page the owner never got.
      await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
      expect(approve).not.toHaveBeenCalled();
      // The two-phase web tab dance must not have run.
      expect(openSpy).not.toHaveBeenCalled();
    } finally {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      delete (window as any).__TAURI__;
    }
  });

  it("desktop (Tauri): approves only after the system browser opened", async () => {
    post.mockResolvedValue({ onboarding_url: "https://connect.stripe.com/setup/x" });
    const invoke = vi.fn().mockResolvedValue(undefined);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__TAURI__ = { core: { invoke } };
    try {
      render(<ApprovalRow approval={A} />);
      fireEvent.click(screen.getByRole("button", { name: "Set up Stripe" }));

      await waitFor(() =>
        expect(invoke).toHaveBeenCalledWith("plugin:shell|open", {
          path: "https://connect.stripe.com/setup/x",
        }),
      );
      await waitFor(() => expect(approve).toHaveBeenCalledWith("a-stripe"));
    } finally {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      delete (window as any).__TAURI__;
    }
  });

  it("Connect existing navigates the tab to the authorize URL", async () => {
    get.mockResolvedValue({ authorize_url: "https://connect.stripe.com/oauth/authorize?x" });
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    render(<ApprovalRow approval={A} />);

    fireEvent.click(screen.getByRole("button", { name: "Connect existing" }));

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
