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
import { headlineCookieScript, headlineVariantProps } from "@/lib/headlineVariant";

beforeEach(() => {
  captureSignupCta.mockReset();
  register.mockReset();
  document.cookie = "lb_hv=; Max-Age=0; Path=/";
});

describe("hero: two doors and the headline test", () => {
  it("links the two buttons and tracks each with the variant", () => {
    render(<Hero variant="B" />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("AI employees your whole team can see.");
    const setup = screen.getByRole("link", { name: /Request a guided setup/ });
    const start = screen.getByRole("link", { name: "Start free" });
    expect(setup).toHaveAttribute("href", "/request-setup");
    expect(start).toHaveAttribute("href", "/sign-up");
    fireEvent.click(setup);
    fireEvent.click(start);
    expect(captureSignupCta.mock.calls.map(([o]) => [o.ctaId, o.properties.headline_variant])).toEqual([
      ["hero_setup", "B"],
      ["hero_start_free", "B"],
    ]);
  });

  it("keeps the first variant in a cookie before PostHog starts, and never overwrites it", () => {
    new Function(headlineCookieScript("A"))();
    expect(document.cookie).toContain("lb_hv=A");
    expect(headlineVariantProps()).toEqual({ headline_variant: "A" });
    new Function(headlineCookieScript("B"))();
    expect(document.cookie).toContain("lb_hv=A");
    expect(document.cookie).not.toContain("lb_hv=B");
  });

  it("renders the cookie script in the hero and registers the variant", () => {
    const { container } = render(<Hero variant="B" />);
    expect(container.querySelector("script")?.innerHTML).toBe(headlineCookieScript("B"));
    expect(register).toHaveBeenCalledWith({ headline_variant: "B" });
  });
});
