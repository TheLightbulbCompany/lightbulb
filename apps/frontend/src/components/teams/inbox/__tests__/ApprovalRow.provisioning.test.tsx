// apps/frontend/src/components/teams/inbox/__tests__/ApprovalRow.provisioning.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const approve = vi.fn();
const reject = vi.fn();
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

const post = vi.fn();
const get = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post, get }) }));

function fakeTab() {
  return { opener: undefined, location: { href: "" }, close: vi.fn() };
}

import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

const PAIR: Approval = {
  id: "a-cli",
  type: "request_board_approval",
  payload: {
    isol8_action: "stripe_cli_login",
    title: "Pair the Stripe CLI",
    browser_url: "https://dashboard.stripe.com/stripecli/confirm_auth?t=x",
    verification_code: "wistful-quiet-abcd",
  },
  status: "pending",
};

const CONSENT: Approval = {
  id: "a-consent",
  type: "request_board_approval",
  payload: {
    isol8_action: "provider_consent",
    title: "PostHog analytics",
    provider: "posthog",
    service: "analytics",
    terms_url: "https://posthog.com/terms",
    pricing: "Free up to 1M events/mo",
  },
  status: "pending",
};

beforeEach(() => {
  approve.mockReset().mockResolvedValue(undefined);
  reject.mockReset().mockResolvedValue(undefined);
  notifyApiError.mockReset();
  notifyError.mockReset();
  post.mockReset();
  get.mockReset();
});

describe("ApprovalRow stripe_cli_login", () => {
  it("shows the code and opens Stripe WITHOUT approving; Approve is the owner's own click", async () => {
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    render(<ApprovalRow approval={PAIR} />);

    expect(screen.getByText("Pair Stripe CLI: Pair the Stripe CLI")).toBeInTheDocument();
    expect(screen.getByText("wistful-quiet-abcd")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open Stripe" }));
    expect(tab.location.href).toBe("https://dashboard.stripe.com/stripecli/confirm_auth?t=x");
    // The owner has to confirm the code in Stripe first — opening is not consent.
    expect(approve).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-cli"));
  });

  it("reports a blocked popup", () => {
    vi.spyOn(window, "open").mockImplementation(() => null);
    render(<ApprovalRow approval={PAIR} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Stripe" }));
    expect(notifyError).toHaveBeenCalled();
    expect(approve).not.toHaveBeenCalled();
  });

  it("refuses to open a payload URL that is not Stripe's dashboard", () => {
    const payload = { ...PAIR.payload, browser_url: "https://evil.example/phish" };
    render(<ApprovalRow approval={{ ...PAIR, payload }} />);
    expect(screen.queryByRole("button", { name: "Open Stripe" })).toBeNull();
    // Shown as inert text so the owner can still see where the agent pointed.
    expect(screen.getByText("https://evil.example/phish")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
  });
});

describe("ApprovalRow provider_consent", () => {
  it("shows provider, service, price and a Terms link, then approves on click", async () => {
    render(<ApprovalRow approval={CONSENT} />);

    expect(screen.getByText("Approve provider: PostHog analytics")).toBeInTheDocument();
    expect(screen.getByText(/posthog · analytics · Free up to 1M events\/mo/)).toBeInTheDocument();
    expect(screen.getByText("Terms")).toHaveAttribute("href", "https://posthog.com/terms");

    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-consent"));
  });

  it("drops a non-https Terms link", () => {
    const payload = { ...CONSENT.payload, terms_url: "javascript:alert(1)" };
    render(<ApprovalRow approval={{ ...CONSENT, payload }} />);
    expect(screen.queryByText("Terms")).toBeNull();
  });
});
