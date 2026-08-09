import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Navbar } from "../Navbar";

const observed: Element[] = [];

class MockIntersectionObserver {
  observe = vi.fn((element: Element) => {
    observed.push(element);
  });
  unobserve = vi.fn();
  disconnect = vi.fn();
}

describe("Navbar", () => {
  beforeEach(() => {
    observed.length = 0;
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
  });

  it("renders landing navigation in the requested desktop and mobile order", () => {
    const { container } = render(<Navbar />);

    const navLinks = Array.from(container.querySelectorAll<HTMLAnchorElement>(".nav-links a[data-section]"))
      .map((link) => link.textContent?.replace(/alpha$/, ""));

    expect(navLinks.slice(0, 8)).toEqual([
      "Home",
      "Team",
      "Marketplace",
      "Features",
      "Pricing",
      "FAQ",
      "GooseTown",
      "Blog",
    ]);
  });

  it("registers scroll-spy sections in the same in-page order", () => {
    document.body.innerHTML = `
      <section class="ld-hero"></section>
      <section id="team"></section>
      <section id="marketplace"></section>
      <section id="features"></section>
      <section id="pricing"></section>
      <section id="faq"></section>
      <section id="goosetown"></section>
    `;

    render(<Navbar />);

    expect(
      observed.map((element) => element.id || (element.classList.contains("ld-hero") ? "home" : "")),
    ).toEqual(["home", "team", "marketplace", "features", "pricing", "faq", "goosetown"]);
  });
});
