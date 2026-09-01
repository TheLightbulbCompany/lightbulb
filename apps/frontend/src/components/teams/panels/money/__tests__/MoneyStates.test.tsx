// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { MoneyStates } from "../MoneyStates";
import { moneyView, type MoneyEnvelope } from "@/hooks/useCompanyMoney";

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
    expect(screen.getByRole("link", { name: /finish stripe setup/i })).toBeInTheDocument();
    expect(screen.queryByText(/No payments yet/i)).not.toBeInTheDocument();
  });

  it("names the noun, so one component serves three tabs without drifting", () => {
    const { rerender } = render(<MoneyStates view={{ kind: "zero" }} noun="payments" />);
    expect(screen.getByText("No payments yet.")).toBeInTheDocument();
    rerender(<MoneyStates view={{ kind: "zero" }} noun="customers" />);
    expect(screen.getByText("No customers yet.")).toBeInTheDocument();
  });

  it("stays calm on every healthy state — only a real failure may be loud", () => {
    // system.md: calm when healthy, loud only on incidents. Unbound, partial
    // and zero are the normal first hour of every account.
    for (const kind of ["unbound", "partial", "zero"] as const) {
      const { container, unmount } = render(<MoneyStates view={{ kind }} noun="payments" />);
      expect(container.querySelector("[data-panel-notice]")!.className).not.toMatch(
        /border-l-err/,
      );
      unmount();
    }
  });
});
