import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { PricingThreeCard } from "../PricingThreeCard";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const captureCta = vi.hoisted(() => vi.fn());
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: captureCta }));

describe("PricingThreeCard", () => {
  it("offers the same workspace foundation without a separate team billing choice", () => {
    render(<PricingThreeCard />);
    const cta = screen.getByRole("link", { name: /create a workspace/i });
    expect(cta).toHaveAttribute("href", "/sign-up");
    for (const link of screen.getAllByRole("link")) expect(link).toHaveAttribute("href", "/sign-up");
    expect(screen.queryAllByText(/per-seat|Lightbulb for Teams/i)).toHaveLength(0);
    cta.addEventListener("click", (event) => event.preventDefault(), { once: true });
    fireEvent.click(cta);
    expect(captureCta).toHaveBeenLastCalledWith({
      ctaId: "pricing_create_workspace", label: "Create a workspace",
      surface: "landing_pricing", destination: "/sign-up",
    });
  });

  it("still renders the three provider cards", () => {
    render(<PricingThreeCard />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Bring your own API key")).toBeInTheDocument();
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
  });
});
