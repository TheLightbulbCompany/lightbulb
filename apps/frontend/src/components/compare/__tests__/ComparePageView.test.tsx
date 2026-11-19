import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import { COMPARE_PAGES, getComparePage } from "@/content/compare";

import { ComparePageView } from "../ComparePageView";

describe("ComparePageView", () => {
  it.each(COMPARE_PAGES.map((p) => [p.slug, p] as const))("%s: table, dated, tagged sign-up, home link", (slug, page) => {
    const { container, unmount } = render(<ComparePageView page={page} />);
    expect(screen.getByRole("heading", { level: 1, name: page.h1 })).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(page.rows.length + 1);
    expect(screen.getByText(/Updated/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set up your assistant" })).toHaveAttribute("href", `/sign-up?source=compare-${slug}`);
    expect(container.querySelector('a[href="/"]')).not.toBeNull();
    unmount();
  });

  it("emits FAQPage JSON-LD that matches the visible questions", () => {
    const page = getComparePage("receptionist")!;
    const { container } = render(<ComparePageView page={page} />);
    const graphs = [...container.querySelectorAll('script[type="application/ld+json"]')].map((s) => JSON.parse(s.textContent!));
    const faq = graphs.find((g) => g["@type"] === "FAQPage");
    expect(faq.mainEntity.map((q: { name: string }) => q.name)).toEqual(page.faq.map((f) => f.q));
    for (const { q } of page.faq) expect(screen.getByText(q)).toBeInTheDocument();
  });

  it("links the BLS source on the receptionist cost row", () => {
    render(<ComparePageView page={getComparePage("receptionist")!} />);
    expect(screen.getByRole("link", { name: /BLS, May 2025/ })).toHaveAttribute(
      "href",
      "https://www.bls.gov/ooh/office-and-administrative-support/receptionists.htm",
    );
  });
});
