// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

/**
 * Regression cover for a live production bug: every landing headline was
 * missing a space on mobile.
 *
 * Each headline is authored as
 *
 *     Three steps between you
 *     <br className="lb-brk" />
 *     and your next hire.
 *
 * JSX trims the whitespace at the end and start of the lines either side of
 * the element, so the two text nodes sit flush against the <br>. That is
 * invisible while the break renders — but `landing-v2.css` sets
 * `.lb-brk { display: none }` below 900px, and the fragments then collide:
 * "Three steps between youand your next hire."
 *
 * Read off live flickmeon.ai at 375px on 2026-09-17, all four were broken:
 *   "Your team and your agents,in one place."
 *   "Three steps between youand your next hire."
 *   "Everything you needto run the company."
 *   "One price.Three ways to power it."
 *
 * The fix is an explicit {" "} before each break — invisible when the break
 * renders, correct when it doesn't. These assertions are width-independent on
 * purpose: `textContent` ignores <br> exactly the way a hidden break does, so
 * the unit test reproduces the mobile rendering without emulating a viewport.
 */

// next/image needs a real loader; the landing bands only use it decoratively.
// next/image's own props (fill, priority, sizes…) are not DOM attributes, so
// they are dropped rather than spread — forwarding them makes React warn on
// every render and buries the actual assertion output in noise.
vi.mock("next/image", () => ({
  default: ({ src, alt = "" }: { src?: string; alt?: string }) =>
    // eslint-disable-next-line @next/next/no-img-element
    <img src={typeof src === "string" ? src : ""} alt={alt} />,
}));

const flatten = (node: HTMLElement) => (node.textContent ?? "").replace(/\s+/g, " ").trim();

describe("landing headlines survive .lb-brk being hidden", () => {
  it("Hero keeps a space after the comma", async () => {
    const { Hero } = await import("../Hero");
    const { container } = render(<Hero />);
    expect(flatten(container)).toContain("Your team and your agents, in one place.");
  });

  it("HowItWorks keeps a space between 'you' and 'and'", async () => {
    const { HowItWorks } = await import("../HowItWorks");
    const { container } = render(<HowItWorks />);
    expect(flatten(container)).toContain("Three steps between you and your next hire.");
  });

  it("WorkspaceBand keeps a space between 'need' and 'to'", async () => {
    const { WorkspaceBand } = await import("../WorkspaceBand");
    const { container } = render(<WorkspaceBand />);
    expect(flatten(container)).toContain("Everything you need to run the company.");
  });

  it("PricingThreeCard keeps a space after the first sentence", async () => {
    const { PricingThreeCard } = await import("../PricingThreeCard");
    const { container } = render(<PricingThreeCard />);
    expect(flatten(container)).toContain("One price. Three ways to power it.");
  });
});
