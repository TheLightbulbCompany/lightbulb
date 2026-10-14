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

  it("keeps the first variant in a cookie and on every event", () => {
    render(<Hero variant="A" />);
    expect(document.cookie).toContain("lb_hv=A");
    expect(register).toHaveBeenCalledWith({ headline_variant: "A" });
  });

  it("never overwrites a variant the visitor already has", () => {
    document.cookie = "lb_hv=B; Path=/";
    render(<Hero variant="A" />);
    expect(document.cookie).toContain("lb_hv=B");
  });
});
