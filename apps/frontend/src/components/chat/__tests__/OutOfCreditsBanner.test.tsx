// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { OutOfCreditsBanner } from "../OutOfCreditsBanner";

const mockCredits = vi.fn();
const mockSwr = vi.fn();
vi.mock("@/hooks/useCredits", () => ({ useCredits: () => mockCredits() }));
vi.mock("swr", () => ({ default: () => mockSwr() }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn() }) }));

beforeEach(() => {
  mockSwr.mockReturnValue({ data: { provider_choice: "bedrock_claude" } });
});

describe("OutOfCreditsBanner", () => {
  it("shows when the balance is unspendable at the gate, not only at zero", () => {
    // The gate refuses paid admission at remaining <= one reservation, so a
    // $0.20 balance buys nothing. The old `> 0` check hid the banner for
    // exactly the owners already being refused.
    mockCredits.mockReturnValue({ balance: { balance_microcents: 200_000, is_exhausted: true } });
    render(<OutOfCreditsBanner />);
    expect(screen.getByText(/out of Claude credits/i)).toBeInTheDocument();
  });

  it("stays hidden on a spendable balance", () => {
    mockCredits.mockReturnValue({ balance: { balance_microcents: 5_000_000, is_exhausted: false } });
    const { container } = render(<OutOfCreditsBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("stays hidden on an indeterminate balance", () => {
    // Metronome outage / no customer yet. A false out-of-credits banner is
    // worse than a late one.
    mockCredits.mockReturnValue({ balance: { balance_microcents: null, is_exhausted: false } });
    const { container } = render(<OutOfCreditsBanner />);
    expect(container).toBeEmptyDOMElement();
  });
});
