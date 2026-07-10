import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { PricingThreeCard } from "../PricingThreeCard";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

describe("PricingThreeCard", () => {
  it("renders a Teams CTA pointing at /sign-up?intent=team", () => {
    render(<PricingThreeCard />);
    const cta = screen.getByRole("link", { name: /create a team/i });
    expect(cta).toHaveAttribute("href", "/sign-up?intent=team");
  });

  it("still renders the three provider cards", () => {
    render(<PricingThreeCard />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Bring your own API key")).toBeInTheDocument();
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
  });
});
