import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { CreditsSection } from "../CreditsSection";

const mockUseCredits = vi.fn();
vi.mock("@/hooks/useCredits", () => ({ useCredits: () => mockUseCredits() }));

describe("CreditsSection", () => {
  beforeEach(() => {
    mockUseCredits.mockReset();
    mockUseCredits.mockReturnValue({
      balance: { balance_microcents: 3_952_000_000, balance_dollars: "39.52" },
      autoChargeEnabled: false,
      refresh: vi.fn(),
      startTopUp: vi.fn(),
      startTopUpCheckout: vi.fn(),
      disableAutoCharge: vi.fn(),
    });
  });

  it("renders the balance and the checkout CTA without a page heading", () => {
    render(<CreditsSection />);
    expect(screen.getByText("$39.52")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /continue to payment — \$20/i }),
    ).toBeInTheDocument();
    // No standalone page <h2> — the section embeds inside Model & Credits.
    expect(screen.queryByRole("heading", { name: /claude credits/i })).toBeNull();
  });
});
