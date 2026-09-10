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
      refresh: vi.fn(),
      startTopUp: vi.fn(),
      canManageCredits: true,
      pendingTopUp: null,
    });
  });

  it("renders the balance and payment-gated top-up CTA without a page heading", () => {
    render(<CreditsSection />);
    expect(screen.getByText("$39.52")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /add \$20/i }),
    ).toBeInTheDocument();
    // No standalone page <h2> — the section embeds inside Model & Credits.
    expect(screen.queryByRole("heading", { name: /claude credits/i })).toBeNull();
  });

  it("keeps the balance visible but makes top-ups read-only for members", () => {
    mockUseCredits.mockReturnValue({
      balance: { balance_microcents: 3_952_000_000, balance_dollars: "39.52" },
      refresh: vi.fn(),
      startTopUp: vi.fn(),
      canManageCredits: false,
      pendingTopUp: null,
    });
    render(<CreditsSection />);
    expect(screen.getByText("$39.52")).toBeInTheDocument();
    expect(screen.getByText("Only workspace admins can add credits.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add \$20/i })).toBeNull();
  });

  it("shows a persisted accepted top-up as locked after a reload", async () => {
    mockUseCredits.mockReturnValue({
      balance: { balance_microcents: 3_952_000_000, balance_dollars: "39.52" },
      refresh: vi.fn(),
      startTopUp: vi.fn(),
      canManageCredits: true,
      pendingTopUp: { amount_cents: 2000, state: "requested" },
    });
    render(<CreditsSection />);
    expect(await screen.findByRole("button", { name: "Top-up requested" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "$50" })).toBeDisabled();
  });
});
