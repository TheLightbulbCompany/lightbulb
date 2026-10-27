// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const captureSignupCta = vi.fn();
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: (o: unknown) => captureSignupCta(o) }));
vi.mock("next/image", () => ({ default: () => null }));

import { Hero } from "../Hero";

beforeEach(() => {
  captureSignupCta.mockReset();
});

describe("hero: one primary door and the trust row", () => {
  it("leads with self-serve, links both buttons and tracks each", () => {
    render(<Hero />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Your team and your AI employees, in one place.",
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
    expect(captureSignupCta.mock.calls.map(([o]) => o.ctaId)).toEqual(["hero_start_free", "hero_setup"]);
  });

  it("shows the trust row under the buttons", () => {
    render(<Hero />);
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "30-day free trial",
      "No credit card required",
      "2 minute setup",
    ]);
  });

  it("carries no headline-test script or cookie", () => {
    const { container } = render(<Hero />);
    expect(container.querySelector("script")).toBeNull();
    expect(document.cookie).not.toContain("lb_hv");
  });
});
