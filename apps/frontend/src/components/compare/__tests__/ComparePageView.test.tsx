import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import { COMPARE_PAGES } from "@/content/compare";

import { ComparePageView } from "../ComparePageView";

describe("ComparePageView", () => {
  it.each(COMPARE_PAGES.map((p) => [p.slug, p] as const))("%s: byline, dated table, both doors tagged", (slug, page) => {
    const { unmount } = render(<ComparePageView page={page} />);
    expect(screen.getByText(/By Leo/)).toBeInTheDocument();
    expect(screen.getByText(/Checked on 2026-10-13/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Request a guided setup" })).toHaveAttribute(
      "href",
      `/request-setup?source=compare-${slug}`,
    );
    expect(screen.getByRole("link", { name: "Start free" })).toHaveAttribute("href", `/sign-up?source=compare-${slug}`);
    unmount();
  });
});
