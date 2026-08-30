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

import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

// The two-phase tab: opened EMPTY and synchronously in the click handler, then
// pointed at the URL the API mints for THAT click.
function fakeTab() {
  return { opener: undefined, location: { href: "" }, close: vi.fn() };
}

function card(payload: Record<string, unknown>, id = "apr_1"): Approval {
  return { id, type: "request_board_approval", status: "pending", payload };
}

const CONSENT: Approval = card(
  {
    isol8_action: "provider_consent",
    workflow_id: "pwf_1",
    title: "PostHog analytics",
    provider: "posthog",
    service: "analytics",
    terms_url: "https://posthog.com/terms",
    price_display: "Free up to 1M events/mo",
    reason: "The launch dashboard needs product analytics",
    environment: "Company — production",
    payment_owner: "your card, on your Stripe account",
    action: "creates new",
    configuration: { region: "us-east-1", seats: 3, sso: true, nested: { no: 1 } },
    usage_limit: { max_amount: 5000, currency: "usd", recurring_interval: "month" },
    usage_limit_editable: true,
  },
  "a-consent",
);

beforeEach(() => {
  approve.mockReset().mockResolvedValue(undefined);
  reject.mockReset().mockResolvedValue(undefined);
  notifyApiError.mockReset();
  notifyError.mockReset();
  post.mockReset().mockResolvedValue({});
  get.mockReset();
});

describe("ApprovalRow provider_consent", () => {
  it("shows the disclosures and a Terms link, then approves with the edited limit", async () => {
    render(<ApprovalRow approval={CONSENT} />);

    expect(screen.getByText("Approve provider: PostHog analytics")).toBeInTheDocument();
    expect(screen.getByText(/posthog · analytics · Free up to 1M events\/mo/)).toBeInTheDocument();
    expect(screen.getByText(/The launch dashboard needs product analytics/)).toBeInTheDocument();
    expect(screen.getByText(/your card, on your Stripe account/)).toBeInTheDocument();
    // Scalars only — a nested value is not a field the router would have filed.
    expect(screen.getByText(/region: us-east-1, seats: 3, sso: true/)).toBeInTheDocument();
    expect(screen.queryByText(/nested/)).toBeNull();
    expect(screen.getByText("Terms")).toHaveAttribute("href", "https://posthog.com/terms");
    expect(screen.getByText("Terms")).toHaveAttribute("rel", "noopener noreferrer");

    fireEvent.change(screen.getByLabelText(/Spending limit/), { target: { value: "25" } });
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/approve", {
        usage_limit: { max_amount: 2500, currency: "usd", recurring_interval: "month" },
      }),
    );
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-consent"));
  });

  it("drops a non-https Terms link", () => {
    render(<ApprovalRow approval={card({ ...CONSENT.payload, terms_url: "javascript:alert(1)" }, "a-consent")} />);
    expect(screen.queryByText("Terms")).toBeNull();
  });

  it("a read-only limit renders as text, not an input", () => {
    render(
      <ApprovalRow
        approval={card(
          {
            ...CONSENT.payload,
            usage_limit_editable: false,
            usage_limit_note: "Change it in Money → Spend.",
          },
          "a-consent",
        )}
      />,
    );
    expect(screen.queryByLabelText(/Spending limit/)).toBeNull();
    expect(screen.getByText(/\$50\.00 \/ month/)).toBeInTheDocument();
    expect(screen.getByText(/Change it in Money → Spend\./)).toBeInTheDocument();
  });

  it("does not approve in Paperclip when our approve route fails", async () => {
    post.mockRejectedValue(new Error("409"));
    render(<ApprovalRow approval={CONSENT} />);
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    expect(approve).not.toHaveBeenCalled();
  });

  it("refuses to approve a card that lost its workflow id", async () => {
    const orphan = { ...CONSENT.payload };
    delete (orphan as Record<string, unknown>).workflow_id;
    render(<ApprovalRow approval={card(orphan, "a-consent")} />);
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(notifyError).toHaveBeenCalled());
    expect(post).not.toHaveBeenCalled();
    expect(approve).not.toHaveBeenCalled();
  });

  it("Reject tells the backend before Paperclip", async () => {
    render(<ApprovalRow approval={CONSENT} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.change(screen.getByLabelText("Reason for rejection"), { target: { value: "too pricey" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm reject" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/reject", {}),
    );
    await waitFor(() => expect(reject).toHaveBeenCalledWith("a-consent", "too pricey"));
  });
});

describe("ApprovalRow provider_consent — reject", () => {
  it("does not reject in Paperclip when our reject route fails", async () => {
    post.mockRejectedValue(new Error("409"));
    render(<ApprovalRow approval={CONSENT} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.change(screen.getByLabelText("Reason for rejection"), { target: { value: "no" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm reject" }));
    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    expect(reject).not.toHaveBeenCalled();
  });
});

describe("ApprovalRow provider_connect", () => {
  const CONNECT = card({
    isol8_action: "provider_connect",
    workflow_id: "pwf_1",
    title: "Connect posthog",
    provider: "posthog",
    provider_account: { display_name: "Northwind", primary_email: "ops@northwind.test" },
  });

  it("mints the URL on click, opens the tab synchronously, and never auto-approves", async () => {
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    post.mockResolvedValue({ redirect_url: "https://provider.example/oauth?fresh" });
    render(<ApprovalRow approval={CONNECT} />);

    expect(screen.getByText(/ops@northwind.test/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Connect posthog" }));
    // Opened BEFORE the await, for Safari.
    expect(window.open).toHaveBeenCalledWith("", "_blank");

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/connect-link", {}),
    );
    await waitFor(() => expect(tab.location.href).toBe("https://provider.example/oauth?fresh"));
    // Opening a tab is not consent — the owner still has to come back and approve.
    expect(approve).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeInTheDocument();
  });

  it("does not call the API when the popup is blocked", async () => {
    vi.spyOn(window, "open").mockImplementation(() => null);
    render(<ApprovalRow approval={CONNECT} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect posthog" }));
    await waitFor(() => expect(notifyError).toHaveBeenCalled());
    expect(post).not.toHaveBeenCalled();
    expect(approve).not.toHaveBeenCalled();
  });

  it("Reject WITHDRAWS the workflow — /reject only accepts a consent card", async () => {
    render(<ApprovalRow approval={CONNECT} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.change(screen.getByLabelText("Reason for rejection"), { target: { value: "wrong service" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm reject" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/cancel", {}),
    );
    expect(post).not.toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/reject", {});
    await waitFor(() => expect(reject).toHaveBeenCalledWith("apr_1", "wrong service"));
  });

  it("still clears the card when the withdrawal is refused, and says why", async () => {
    const refusal = Object.assign(new Error("This request has already started — review it instead"), {
      status: 409,
    });
    post.mockRejectedValue(refusal);
    render(<ApprovalRow approval={CONNECT} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.change(screen.getByLabelText("Reason for rejection"), { target: { value: "no" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm reject" }));
    await waitFor(() => expect(reject).toHaveBeenCalledWith("apr_1", "no"));
    expect(notifyError).toHaveBeenCalledWith("This request has already started — review it instead");
  });
});

describe("ApprovalRow payment_setup", () => {
  const PAYMENT = card({
    isol8_action: "payment_setup",
    workflow_id: "pwf_1",
    title: "Add a card for posthog",
    provider: "posthog",
    price_display: "$20/mo",
    usage_limit: { max_amount: 5000, currency: "usd", recurring_interval: "month" },
  });

  it("mints the checkout URL on click and opens it, without approving", async () => {
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    post.mockResolvedValue({ checkout_session_url: "https://checkout.stripe.com/x", status: "checkout_initiated" });
    render(<ApprovalRow approval={PAYMENT} />);

    expect(screen.getByText(/\$50\.00 \/ month/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add a card" }));
    expect(window.open).toHaveBeenCalledWith("", "_blank");

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/payment-link", {}),
    );
    await waitFor(() => expect(tab.location.href).toBe("https://checkout.stripe.com/x"));
    expect(approve).not.toHaveBeenCalled();
  });

  it("closes the tab and says so when a card is already on file", async () => {
    const tab = fakeTab();
    vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    post.mockResolvedValue({ checkout_session_url: null, status: "complete" });
    render(<ApprovalRow approval={PAYMENT} />);
    fireEvent.click(screen.getByRole("button", { name: "Add a card" }));
    await waitFor(() => expect(tab.close).toHaveBeenCalled());
    expect(notifyError).toHaveBeenCalled();
    expect(tab.location.href).toBe("");
  });

  it("Reject withdraws the payment row too", async () => {
    render(<ApprovalRow approval={PAYMENT} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.change(screen.getByLabelText("Reason for rejection"), { target: { value: "not now" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm reject" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/cancel", {}),
    );
    await waitFor(() => expect(reject).toHaveBeenCalled());
  });
});

describe("ApprovalRow provider_information", () => {
  const SCHEMA = {
    type: "object",
    properties: {
      team: { type: "string" },
      seats: { type: "number" },
      tier: { enum: ["free", "pro"] },
      on: { type: "boolean" },
    },
    required: ["team"],
  };
  const INFO = card({
    isol8_action: "provider_information",
    workflow_id: "pwf_1",
    title: "posthog needs more information",
    provider: "posthog",
    schema: SCHEMA,
    target: "account_request",
  });

  it("renders only the schema shapes we support", () => {
    render(<ApprovalRow approval={INFO} />);
    expect(screen.getByLabelText("team")).toHaveAttribute("type", "text");
    expect(screen.getByLabelText("team")).toBeRequired();
    expect(screen.getByLabelText("seats")).toHaveAttribute("type", "number");
    expect(screen.getByLabelText("tier").tagName).toBe("SELECT");
    expect(screen.getByLabelText("on")).toHaveAttribute("type", "checkbox");
  });

  it("submits the answers, then approves", async () => {
    render(<ApprovalRow approval={INFO} />);
    fireEvent.change(screen.getByLabelText("team"), { target: { value: "Northwind" } });
    fireEvent.change(screen.getByLabelText("seats"), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText("tier"), { target: { value: "pro" } });
    fireEvent.click(screen.getByLabelText("on"));
    fireEvent.click(screen.getByRole("button", { name: "Send to posthog" }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/information", {
        information: { team: "Northwind", seats: 4, tier: "pro", on: true },
      }),
    );
    await waitFor(() => expect(approve).toHaveBeenCalledWith("apr_1"));
  });

  it("will not send until every required field is answered", () => {
    render(
      <ApprovalRow
        approval={card({
          isol8_action: "provider_information",
          workflow_id: "pwf_1",
          provider: "posthog",
          schema: { type: "object", properties: { seats: { type: "number" } }, required: ["seats"] },
        })}
      />,
    );
    // Empty required number: never coerced to 0, and never sendable.
    expect(screen.getByRole("button", { name: "Send to posthog" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("seats"), { target: { value: "4" } });
    expect(screen.getByRole("button", { name: "Send to posthog" })).toBeEnabled();
  });

  it("refuses to submit a card that lost its workflow id", async () => {
    // Same guard as the consent card: with no id there is nothing to submit to,
    // and approving anyway would tell the owner the provider has their answers.
    const orphan = card({ ...INFO.payload, workflow_id: "" }, "a-orphan-info");
    render(<ApprovalRow approval={orphan} />);
    fireEvent.change(screen.getByLabelText("team"), { target: { value: "Northwind" } });
    fireEvent.click(screen.getByRole("button", { name: "Send to posthog" }));
    await waitFor(() => expect(notifyError).toHaveBeenCalled());
    expect(post).not.toHaveBeenCalled();
    expect(approve).not.toHaveBeenCalled();
  });

  it("leaves untouched optional fields out of the answer", async () => {
    render(<ApprovalRow approval={INFO} />);
    fireEvent.change(screen.getByLabelText("team"), { target: { value: "Northwind" } });
    fireEvent.click(screen.getByRole("button", { name: "Send to posthog" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/workflows/pwf_1/information", {
        information: { team: "Northwind" },
      }),
    );
  });

  it("shows the field the backend rejected, and does not approve", async () => {
    post.mockRejectedValue(
      Object.assign(new Error("$.team is not valid for this form"), {
        status: 422,
        body: { detail: { code: "invalid_information", message: "$.team is not valid for this form" } },
      }),
    );
    render(<ApprovalRow approval={INFO} />);
    fireEvent.change(screen.getByLabelText("team"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Send to posthog" }));
    expect(await screen.findByText(/\$\.team is not valid for this form/)).toBeInTheDocument();
    expect(approve).not.toHaveBeenCalled();
  });

  it("refuses a schema shape it cannot render, and offers no submit", () => {
    render(
      <ApprovalRow
        approval={card({
          isol8_action: "provider_information",
          workflow_id: "pwf_1",
          provider: "posthog",
          schema: { type: "object", properties: { region: { type: "object" } } },
        })}
      />,
    );
    expect(screen.getByText(/needs Lightbulb/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send to posthog" })).toBeNull();
  });
});
