import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RoomLimitBar } from "../RoomLimitBar";
import type { BillingAccount } from "@/hooks/useBilling";

vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ account: null }) }));

const account = (over: Partial<BillingAccount>): BillingAccount => ({
  is_subscribed: false, current_spend: 0, lifetime_spend: 0, subscription_status: "trial_local", trial_end: null,
  provider_choice: null, byo_provider: null, workspace_price_usd: 50, base_price_usd: 20, ultra_price_usd: 150, ...over,
});

describe("RoomLimitBar", () => {
  it.each([
    [{ block_reason: "trial_expired" as const }, "This workspace is reading-only", "people can still message. Agents start again when an admin picks the plan.", "Continue for $50", "/settings?panel=billing"],
    [{ block_reason: "trial_daily_cap" as const }, "Daily free limit reached", "resets at 00:00 UTC", "Upgrade to keep going", "/settings?panel=billing"],
    [{ block_reason: "included_daily_cap" as const, is_subscribed: true, subscription_status: "active", plan: "base" as const },
      "Today's included usage is used up", "resets at 00:00 UTC", "Upgrade", "/settings?panel=billing"],
  ])("pins the reason, what it means for agents and the CTA (%o)", (over, title, line, cta, href) => {
    render(<RoomLimitBar account={account(over)} />);
    const bar = screen.getByRole("status");
    expect(bar).toHaveTextContent(title);
    expect(bar).toHaveTextContent(line);
    expect(bar).toHaveTextContent(/Messages to people still send\.|people can still message/);
    expect(screen.getByRole("link", { name: cta })).toHaveAttribute("href", href);
  });

  it("leaves out_of_credits to the shell's OutOfCreditsBanner", () => {
    expect(render(<RoomLimitBar account={account({ block_reason: "out_of_credits" })} />).container).toBeEmptyDOMElement();
  });

  it("renders nothing while agents can run", () => {
    const { container } = render(<RoomLimitBar account={account({ block_reason: null })} />);
    expect(container).toBeEmptyDOMElement();
    expect(render(<RoomLimitBar account={null} />).container).toBeEmptyDOMElement();
  });
});
