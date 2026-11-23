// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));
vi.mock("../PricingCta", () => ({ PricingCta: () => null }));

import PricingPage from "../page";
import { GET as pricingMd } from "../../pricing.md/route";

const VOICE = { voice_plan_monthly_usd: 250, voice_included_minutes: 1500, voice_overage_usd_per_minute: 0.35 };

// Both backend reads: GET /billing/prices (the Phone plan) and GET /os/config (Spark). `spark` null: down.
const stub = (spark: unknown = { priceText: "$20/month", trialDays: 3 }) =>
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.endsWith("/billing/prices")) return { ok: true, json: async () => VOICE };
      if (spark === null) throw new Error("down");
      return { ok: true, json: async () => spark };
    }),
  );

const block = () => screen.getByRole("navigation", { name: "Pricing for each product" });

beforeEach(() => stub());
afterEach(() => vi.unstubAllGlobals());

describe("/pricing", () => {
  it("names all three products, each with its price line and its page", async () => {
    render(await PricingPage());
    const links = [...block().querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href")]);
    expect(links).toEqual([
      ["Lightbulb Phone", "/phone"],
      ["Spark for Mac", "/os"],
      ["Lightbulb Meetings", "/meetings"],
    ]);
    const text = block().textContent ?? "";
    expect(text).toContain("$250 a month includes 1,500 minutes, then $0.35 a minute.");
    expect(text).toContain("Free for 3 days, then $20/month.");
    expect(text).toContain("Waitlist. Not on sale yet.");
    expect(text).not.toMatch(/[—–]/);
    // The Phone page itself is kept.
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("One plan. Your phone answered and booked.");
  });

  it("without Spark's config, no Spark number", async () => {
    stub(null);
    render(await PricingPage());
    expect(block().textContent).toContain("See the price on the Spark page.");
    expect(block().textContent).not.toMatch(/\d+ days/);
  });

  it("/pricing.md lists Spark's price and Meetings", async () => {
    const md = await (await pricingMd()).text();
    expect(md).toContain("- Spark, a voice app for the Mac: Free for 3 days, then $20/month. https://thelightbulb.company/os");
    expect(md).toContain("- Lightbulb Meetings: waitlist, not on sale yet. https://thelightbulb.company/meetings");
  });
});
