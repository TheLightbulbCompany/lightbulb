import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { PricingThreeCard } from "../PricingThreeCard";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

describe("PricingThreeCard", () => {
  it("renders a Teams CTA pointing at /sign-up", () => {
    render(<PricingThreeCard />);
    const cta = screen.getByRole("link", { name: /create a workspace/i });
    expect(cta).toHaveAttribute("href", "/sign-up");
    for (const link of screen.getAllByRole("link")) expect(link).toHaveAttribute("href", "/sign-up");
  });

  it("still renders the three provider cards", () => {
    render(<PricingThreeCard />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Bring your own API key")).toBeInTheDocument();
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
  });
});
