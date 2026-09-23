// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { OutOfCreditsBanner } from "../OutOfCreditsBanner";

const mockCredits = vi.fn();
const mockSwr = vi.fn();
vi.mock("@/hooks/useCredits", async () => {
  // Keep the real isCreditsExhausted so this test exercises the actual
  // predicate (and stays byte-identical with CreditsStep's), not a
  // hand-rolled reimplementation that could silently drift from it.
  const actual = await vi.importActual<typeof import("@/hooks/useCredits")>(
    "@/hooks/useCredits",
  );
  return { ...actual, useCredits: () => mockCredits() };
});
vi.mock("swr", () => ({ default: () => mockSwr() }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn() }) }));

beforeEach(() => {
  mockSwr.mockReturnValue({ data: { provider_choice: "openrouter_managed" } });
});

describe("OutOfCreditsBanner", () => {
  it("shows when the balance is unspendable at the gate, not only at zero", () => {
    // The gate refuses paid admission at remaining <= one reservation, so a
    // $0.20 balance buys nothing. The old `> 0` check hid the banner for
    // exactly the owners already being refused.
    mockCredits.mockReturnValue({ balance: { balance_microcents: 200_000, is_exhausted: true } });
    render(<OutOfCreditsBanner />);
    expect(screen.getByText(/out of workspace credits/i)).toBeInTheDocument();
  });

  it("routes a managed-model owner to workspace credits", () => {
    mockSwr.mockReturnValue({ data: { provider_choice: "openrouter_managed" } });
    mockCredits.mockReturnValue({ balance: { balance_microcents: 0, is_exhausted: true } });
    render(<OutOfCreditsBanner />);
    expect(screen.getByText(/out of workspace credits/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Top up now" })).toHaveAttribute("href", "/chat?panel=llm");
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

  it("falls back to the legacy zero-balance check when is_exhausted is absent (deploy skew)", () => {
    // Frontend (Vercel) deploys before the backend (ECR/ECS rolling deploy).
    // In that window /billing/credits/balance from the OLD backend has no
    // is_exhausted field at all — must not read as "not exhausted" (which
    // would hide the banner from a genuinely zero-balance owner, a
    // regression against pre-branch behaviour).
    mockCredits.mockReturnValue({ balance: { balance_microcents: 0 } });
    render(<OutOfCreditsBanner />);
    expect(screen.getByText(/out of workspace credits/i)).toBeInTheDocument();
  });

  it("stays hidden on a positive balance when is_exhausted is absent (deploy skew)", () => {
    mockCredits.mockReturnValue({ balance: { balance_microcents: 5_000_000 } });
    const { container } = render(<OutOfCreditsBanner />);
    expect(container).toBeEmptyDOMElement();
  });
});
