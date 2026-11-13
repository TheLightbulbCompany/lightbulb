import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));
const getPricing = vi.fn();
vi.mock("@/lib/pricing", async (orig) => ({ ...(await orig<typeof import("@/lib/pricing")>()), getPricing: () => getPricing() }));

import { COMPARE_PAGES, getComparePage } from "@/content/compare";

import { ComparePageView } from "../ComparePageView";

const PRICING = { teamPriceUsd: 50, includedMembers: 5, seatPriceUsd: 20, trialDays: 30, frontDeskPriceUsd: null, managedMarkup: "1.2", voiceMonthlyUsd: 250, voiceIncludedMinutes: 1500, voiceOverageUsd: 0.35 };

beforeEach(() => getPricing.mockResolvedValue(PRICING));

describe("ComparePageView", () => {
  it.each(COMPARE_PAGES.map((p) => [p.slug, p] as const))("%s: byline, dated table, both doors tagged", async (slug, page) => {
    const { unmount } = render(await ComparePageView({ page }));
    expect(screen.getByText(/By Leo/)).toBeInTheDocument();
    expect(screen.getByText(/Checked on 2026-10-13/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Request a guided setup" })).toHaveAttribute(
      "href",
      `/request-setup?source=compare-${slug}`,
    );
    expect(screen.getByRole("link", { name: "Start free" })).toHaveAttribute("href", `/sign-up?source=compare-${slug}`);
    unmount();
  });

  it("prices the Lightbulb column and the price question from the billing API", async () => {
    render(await ComparePageView({ page: getComparePage("slack")! }));
    expect(screen.getByText(/\$110 a month \(\$50 for up to 5 people, \$20 each after\)/)).toBeInTheDocument();
    expect(screen.getByText(/Lightbulb is \$110 a month for the workspace/)).toBeInTheDocument();
  });

  it("names no Lightbulb price when the billing API cannot be read", async () => {
    getPricing.mockResolvedValue(null);
    render(await ComparePageView({ page: getComparePage("slack")! }));
    expect(screen.getByText("One price for the workspace; see pricing")).toBeInTheDocument();
    expect(screen.queryByText(/What does it cost for 8 people/)).toBeNull();
  });
});
