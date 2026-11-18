// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const captureSignupCta = vi.fn();
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: (o: unknown) => captureSignupCta(o) }));

import { Hero } from "../Hero";

beforeEach(() => {
  captureSignupCta.mockReset();
});

describe("hero: one primary door and the trust row", () => {
  it("leads with sign-up, offers the voices second, and tracks the sign-up click", () => {
    render(<Hero />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Your phone, answered and booked while your hands are busy.",
    );
    const [start, voices] = screen.getAllByRole("link");
    expect(start).toHaveAccessibleName(/Set up your assistant/);
    expect(start).toHaveClass("lb-btn-p");
    expect(start).toHaveAttribute("href", "/sign-up");
    expect(voices).toHaveAccessibleName("Hear the voices");
    expect(voices).toHaveClass("lb-btn-s");
    expect(voices).toHaveAttribute("href", "#voices");
    fireEvent.click(start);
    expect(captureSignupCta.mock.calls.map(([o]) => o.ctaId)).toEqual(["hero_start_free"]);
  });

  it("shows the trust row under the buttons", () => {
    render(<Hero />);
    expect(screen.getAllByRole("listitem").slice(0, 3).map((li) => li.textContent)).toEqual([
      "Keep your number",
      "Month to month",
      "English and Spanish",
    ]);
  });
});
