// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { MoneyStates } from "../MoneyStates";
import { moneyView, type MoneyEnvelope } from "@/hooks/useCompanyMoney";
import { ApiError } from "@lightbulb/shared";
import { formatStripeAmount } from "../money";

const env = (over: Partial<MoneyEnvelope<unknown>> = {}): MoneyEnvelope<unknown> => ({
  stripe_bound: true,
  data: [],
  has_more: false,
  next_cursor: null,
  details_submitted: true,
  charges_enabled: true,
  payouts_enabled: true,
  ...over,
});

describe("moneyView", () => {
  it("reads unbound off the flag, not off an empty list", () => {
    // Empty-because-unbound and empty-because-nothing-sold are different
    // sentences to the owner, and `data.length` cannot tell them apart.
    expect(moneyView(env({ stripe_bound: false }), undefined, false)).toEqual({ kind: "unbound" });
  });

  it("distinguishes bound-but-unonboarded from genuinely empty", () => {
    // The live dev account is exactly this: stripe_bound true, all three
    // onboarding booleans false, lists legitimately empty.
    expect(moneyView(env({ details_submitted: false }), undefined, false)).toEqual({
      kind: "partial",
    });
    expect(moneyView(env(), undefined, false)).toEqual({ kind: "zero" });
  });

  it("treats an ABSENT details_submitted as onboarded, not as unonboarded", () => {
    // The field is missing on a not-yet-redeployed backend. Absent must not
    // read as false, or every owner sees "finish setup" during a deploy skew.
    const { details_submitted: _omit, ...withoutField } = env();
    expect(moneyView(withoutField as MoneyEnvelope<unknown>, undefined, false)).toEqual({
      kind: "zero",
    });
  });

  it("surfaces an error rather than an empty table", () => {
    // A list rendering "no payments" during a Stripe outage is a lie the
    // owner would act on.
    expect(moneyView(undefined, new Error("502"), false)).toEqual({ kind: "unavailable" });
  });
});

describe("MoneyStates copy", () => {
  it("tells an unonboarded owner to finish setup, NOT that they have no sales", () => {
    // The failure this component exists to prevent: three tabs all saying
    // "no payments yet" when the truth is a two-minute form away.
    render(<MoneyStates view={{ kind: "partial" }} noun="payments" />);
    expect(screen.getByText(/Stripe onboarding isn't finished/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open accounts/i })).toBeInTheDocument();
    expect(screen.queryByText(/No payments yet/i)).not.toBeInTheDocument();
  });

  it("names the noun, so the sentence is right whichever list is blocked", () => {
    const { rerender } = render(<MoneyStates view={{ kind: "unavailable" }} noun="payments" />);
    expect(screen.getByText(/load your payments/i)).toBeInTheDocument();
    rerender(<MoneyStates view={{ kind: "unavailable" }} noun="customers" />);
    expect(screen.getByText(/load your customers/i)).toBeInTheDocument();
  });

  it("renders nothing for a healthy account — the sections own their empty lines", () => {
    // `zero` (and `rows`/`loading`) never reach this component: MoneyPanel
    // shows the sections for those, each with its own empty line. A notice
    // here would be a second, blunter answer stacked on top of them.
    const { container } = render(<MoneyStates view={{ kind: "zero" }} noun="payments" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("stays calm on every healthy state — only a real failure may be loud", () => {
    // system.md: calm when healthy, loud only on incidents. Unbound and
    // partial are the normal first hour of every account.
    for (const kind of ["unbound", "partial"] as const) {
      const { container, unmount } = render(<MoneyStates view={{ kind }} noun="payments" />);
      expect(container.querySelector("[data-panel-notice]")!.className).not.toMatch(
        /border-l-err/,
      );
      unmount();
    }
  });
});


describe("moneyView — states found in review", () => {
  it("separates a 403 from a Stripe outage", () => {
    // `company_money._owner()` calls require_org_admin, so EVERY non-admin org
    // member 403s on all four routes. Collapsing that into "couldn't reach
    // Stripe, check back shortly" tells them to wait for something permanent.
    expect(moneyView(undefined, new ApiError(403, {}), false)).toEqual({ kind: "forbidden" });
    expect(moneyView(undefined, new ApiError(502, {}), false)).toEqual({ kind: "unavailable" });
  });

  it("does not call a submitted-but-unreviewed account 'connected and ready'", () => {
    // details_submitted true + charges_enabled false. Lists are legitimately
    // empty, but no payment CAN land — and Accounts calls this "Under review",
    // so `zero` here would make two tabs contradict each other.
    expect(
      moneyView(env({ details_submitted: true, charges_enabled: false }), undefined, false),
    ).toEqual({ kind: "under_review" });
  });

  it("treats an ABSENT charges_enabled as enabled, not as under review", () => {
    // Same deploy-skew rule as details_submitted: absent must not read as false.
    const { charges_enabled: _omit, ...withoutField } = env();
    expect(moneyView(withoutField as MoneyEnvelope<unknown>, undefined, false)).toEqual({
      kind: "zero",
    });
  });
});

describe("MoneyStates copy — states found in review", () => {
  it("tells a non-admin the truth, and offers no CTA they cannot act on", () => {
    render(<MoneyStates view={{ kind: "forbidden" }} noun="payments" />);
    expect(screen.getByText(/Only workspace admins can see payments/i)).toBeInTheDocument();
    expect(screen.queryByText(/check back shortly/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("never promises 'Finish Stripe setup', which does not exist for a connected account", () => {
    // CompanyStripeSection gates its Finish onboarding button on
    // source === "created", so that label would send a connect-existing owner
    // to a page without the button it just named.
    render(<MoneyStates view={{ kind: "partial" }} noun="payments" />);
    expect(screen.getByRole("link", { name: /open accounts/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /finish stripe setup/i })).not.toBeInTheDocument();
  });
});

describe("formatStripeAmount", () => {
  it("is not 100x wrong on a zero-decimal currency", () => {
    // JPY has no minor unit: amount 1000 IS 1000 yen. Dividing by 100 rendered
    // "10.00 JPY" on the one surface whose value is reconciling against Stripe.
    expect(formatStripeAmount(1000, "jpy")).toBe("¥1,000");
    expect(formatStripeAmount(1000, "usd")).toBe("$10.00");
  });

  it("groups thousands, which the hand-rolled formatter did not", () => {
    expect(formatStripeAmount(123450, "usd")).toBe("$1,234.50");
  });

  it("does not silently format an unknown currency as dollars", () => {
    expect(formatStripeAmount(500, null)).toBe("500");
  });
});
