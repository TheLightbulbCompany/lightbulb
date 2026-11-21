// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import OsPage from "../page";

const s = vi.hoisted(() => ({ query: "" }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(s.query) }));
vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));
vi.mock("@/components/requestSetup/RequestSetupForm", () => ({ RequestSetupForm: () => null }));

const INVITE = "Open Spark to apply this invite";

describe("/os invite link", () => {
  it("a valid referral code links to the app with it", () => {
    s.query = "ref=abc123XYZ";
    render(<OsPage />);
    expect(screen.getByRole("link", { name: INVITE })).toHaveAttribute("href", "spark://referral?code=abc123XYZ");
  });

  it.each(["", "ref=", "ref=a.b", "ref=user_a", "ref=%3Cx%3E", `ref=${"x".repeat(65)}`])("%s: no invite link", (query) => {
    s.query = query;
    render(<OsPage />);
    expect(screen.queryByRole("link", { name: INVITE })).toBeNull();
  });
});
