import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

// The ProviderPicker is currently defined as a non-exported function inside
// ProvisioningStepper.tsx. To test it directly we'll export it from that
// file (see Step 3 of this task) and import it here.
import { ProviderPicker } from "../ProvisioningStepper";

// useApi is invoked inside the component on Pick — stub it.
vi.mock("@/lib/api", () => ({
  useApi: () => ({
    post: vi.fn(),
    get: vi.fn(),
  }),
}));

// Checkout goes through useBilling.startTrialCheckout — the SAME call the
// settings Billing panel makes. It used to be a second inline api.post here,
// which is how the two surfaces drifted apart in the first place.
const startTrialCheckout = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    startTrialCheckout,
    account: { workspace_price_usd: 50, ultra_price_usd: 150 },
  }),
}));

describe("ProviderPicker", () => {
  it("renders all three provider cards for personal users", () => {
    render(<ProviderPicker isOrg={false} />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Bring your own API key")).toBeInTheDocument();
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
  });

  it("shows the ChatGPT OAuth card to org users too, with the connector hint", () => {
    // New contract: the org owner's ChatGPT account backs the org container
    // (one account ↔ one container), so org users see all three cards. The
    // org-only hint under the ChatGPT card spells out the connector model.
    render(<ProviderPicker isOrg={true} orgName="Acme" />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Bring your own API key")).toBeInTheDocument();
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
    expect(
      screen.getByText(/You connect your ChatGPT account/i),
    ).toBeInTheDocument();
  });

  it("omits the org-only ChatGPT connector hint for personal users", () => {
    render(<ProviderPicker isOrg={false} />);
    expect(
      screen.queryByText(/You connect your ChatGPT account/i),
    ).not.toBeInTheDocument();
  });

  it('uses the "Three ways" headline for both personal and org users', () => {
    render(<ProviderPicker isOrg={false} />);
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(
      "One price. Three ways to power it.",
    );

    render(<ProviderPicker isOrg={true} orgName="Acme" />);
    const headings = screen.getAllByRole("heading", { level: 2 });
    expect(headings[headings.length - 1]).toHaveTextContent(
      "One price. Three ways to power it.",
    );
  });

  it("sizes the 3-col grid on the CONTAINER, not the viewport", () => {
    // This renders inside a resizable dock panel (ChatErrorCard's inline
    // paywall) as well as full-screen. A viewport breakpoint would force three
    // cards into a narrow chat column on any wide desktop; the container query
    // stacks them there and goes three-wide only where there is room.
    for (const props of [{ isOrg: true, orgName: "Acme" }, { isOrg: false }]) {
      const { container } = render(<ProviderPicker {...props} />);
      expect(container.querySelector(".\\@container")).toBeInTheDocument();
      expect(container.querySelector(".\\@3xl\\:grid-cols-3")).toBeInTheDocument();
      expect(container.querySelector(".md\\:grid-cols-3")).not.toBeInTheDocument();
    }
  });
});

// ---------------------------------------------------------------------------
// Starter Claude credits (bedrock_claude only)
// ---------------------------------------------------------------------------
//
// The owner picks how much to start with. The displayed total and the amount
// sent to Checkout must move together — a mismatch here charges a number the
// customer never agreed to, which is the exact failure the combined Checkout
// was built to prevent.

describe("ProviderPicker — starter credits", () => {
  beforeEach(() => startTrialCheckout.mockClear());

  it("defaults to $20 and quotes the combined total, not the bare subscription", () => {
    render(<ProviderPicker isOrg={false} />);
    expect(screen.getByText("$70 at checkout, then $50 / month")).toBeInTheDocument();
    expect(screen.getByText(/Includes \$20 Claude credits/)).toBeInTheDocument();
  });

  it("re-quotes the total when a preset is picked, and sends that amount", () => {
    render(<ProviderPicker isOrg={false} />);
    fireEvent.click(screen.getByRole("button", { name: "$100" }));
    // $50 subscription + $100 credits.
    expect(screen.getByText("$150 at checkout, then $50 / month")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /powered by claude/i }));
    expect(startTrialCheckout).toHaveBeenCalledWith("bedrock_claude", 10000);
  });

  it("clamps a custom amount below the $5 minimum on blur", () => {
    render(<ProviderPicker isOrg={false} />);
    const input = screen.getByLabelText("Starter Claude credits in dollars");
    fireEvent.change(input, { target: { value: "1" } });
    fireEvent.blur(input, { target: { value: "1" } });
    expect(screen.getByText("$55 at checkout, then $50 / month")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /powered by claude/i }));
    expect(startTrialCheckout).toHaveBeenCalledWith("bedrock_claude", 500);
  });

  it("clamps a custom amount above the $1,000 ceiling on blur", () => {
    render(<ProviderPicker isOrg={false} />);
    const input = screen.getByLabelText("Starter Claude credits in dollars");
    fireEvent.change(input, { target: { value: "999999" } });
    fireEvent.blur(input, { target: { value: "999999" } });
    fireEvent.click(screen.getByRole("button", { name: /powered by claude/i }));
    expect(startTrialCheckout).toHaveBeenCalledWith("bedrock_claude", 100000);
  });

  it("does not attach a credit amount to the two providers that spend none", () => {
    render(<ProviderPicker isOrg={false} />);
    fireEvent.click(screen.getByRole("button", { name: /sign in with chatgpt/i }));
    expect(startTrialCheckout).toHaveBeenCalledWith("chatgpt_oauth", undefined);
  });

  it("offers the chooser only on the Bedrock card", () => {
    render(<ProviderPicker isOrg={false} />);
    expect(screen.getAllByLabelText("Starter Claude credits in dollars")).toHaveLength(1);
  });

  it("restores the previous amount when the custom box is cleared", () => {
    // Number("") is 0, not NaN, so the finite-check let a cleared box through
    // and the clamp rewrote it to the $5 floor. Someone who picked $100 and
    // select-all-deleted to retype would have bought $5.
    render(<ProviderPicker isOrg={false} />);
    fireEvent.click(screen.getByRole("button", { name: "$100" }));
    const input = screen.getByLabelText("Starter Claude credits in dollars");
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.blur(input, { target: { value: "" } });
    expect(screen.getByText("$150 at checkout, then $50 / month")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /powered by claude/i }));
    expect(startTrialCheckout).toHaveBeenCalledWith("bedrock_claude", 10000);
  });

  it("never renders float noise in the pre-payment price", () => {
    // The price line is what the customer agrees to. Built as
    // `$${50 + cents/100}` it rendered "$60.230000000000004" for 10.23.
    render(<ProviderPicker isOrg={false} />);
    const input = screen.getByLabelText("Starter Claude credits in dollars");
    for (const typed of ["10.23", "5.02", "25.50"]) {
      fireEvent.change(input, { target: { value: typed } });
      fireEvent.blur(input, { target: { value: typed } });
      expect(screen.queryByText(/\d\.\d{3,}/)).not.toBeInTheDocument();
    }
  });
});
