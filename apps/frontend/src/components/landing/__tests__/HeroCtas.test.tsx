// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const captureSignupCta = vi.fn();
const register = vi.fn();
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: (o: unknown) => captureSignupCta(o) }));
vi.mock("@/lib/analytics", () => ({ register: (p: unknown) => register(p) }));
vi.mock("next/image", () => ({ default: () => null }));

import { Hero } from "../Hero";
import { headlineCookieScript, headlineVariantProps, parseHeadlineVariant } from "@/lib/headlineVariant";

beforeEach(() => {
  captureSignupCta.mockReset();
  register.mockReset();
  document.cookie = "lb_hv=; Max-Age=0; Path=/";
});

describe("hero: one primary door, the trust row and the headline test", () => {
  it("leads with self-serve, links both buttons and tracks each with the variant", () => {
    render(<Hero variant="C" />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Autonomous AI agents that live in your team’s chat and do the work.",
    );
    const [start, setup] = screen.getAllByRole("link");
    expect(start).toHaveAccessibleName(/Try Lightbulb free/);
    expect(start).toHaveClass("lb-btn-p");
    expect(start).toHaveAttribute("href", "/sign-up");
    expect(setup).toHaveAccessibleName("Request a guided setup");
    expect(setup).toHaveClass("lb-btn-s");
    expect(setup).toHaveAttribute("href", "/request-setup");
    fireEvent.click(start);
    fireEvent.click(setup);
    expect(captureSignupCta.mock.calls.map(([o]) => [o.ctaId, o.properties.headline_variant])).toEqual([
      ["hero_start_free", "C"],
      ["hero_setup", "C"],
    ]);
  });

  it("shows the trust row under the buttons", () => {
    render(<Hero variant="A" />);
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "30-day free trial",
      "No credit card required",
      "2 minute setup",
    ]);
  });

  it("treats a cookie from the retired B headline as unassigned", () => {
    expect(parseHeadlineVariant("B")).toBeNull();
    document.cookie = "lb_hv=B; Path=/";
    new Function(headlineCookieScript("C"))();
    expect(document.cookie).toContain("lb_hv=C");
  });

  it("keeps the first variant in a cookie before PostHog starts, and never overwrites it", () => {
    new Function(headlineCookieScript("A"))();
    expect(document.cookie).toContain("lb_hv=A");
    expect(headlineVariantProps()).toEqual({ headline_variant: "A" });
    new Function(headlineCookieScript("C"))();
    expect(document.cookie).toContain("lb_hv=A");
    expect(document.cookie).not.toContain("lb_hv=C");
  });

  it("renders the cookie script in the hero and registers the variant", () => {
    const { container } = render(<Hero variant="C" />);
    expect(container.querySelector("script")?.innerHTML).toBe(headlineCookieScript("C"));
    expect(register).toHaveBeenCalledWith({ headline_variant: "C" });
  });
});
