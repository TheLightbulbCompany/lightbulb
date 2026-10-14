import { afterEach, describe, expect, it, vi } from "vitest";

import { getPricing, markupPercent, ordinal, pricingFaq, teamMonthlyUsd } from "../pricing";

const json = (body: unknown) => new Response(JSON.stringify(body));
const API = { workspace_price_usd: 50, included_members: 5, seat_price_usd: 20, trial_days: 30, managed_markup: "1.2" };
const P = { teamPriceUsd: 50, includedMembers: 5, seatPriceUsd: 20, trialDays: 30, managedMarkup: "1.2" };

afterEach(() => vi.unstubAllGlobals());

describe("one plan pricing", () => {
  it("reads the price from the billing API", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(API)));
    expect(await getPricing()).toEqual(P);
  });

  it("returns null, never a guessed number, when the API is unreachable or malformed", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("down", { status: 503 })));
    expect(await getPricing()).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => json({ ...API, managed_markup: null })));
    expect(await getPricing()).toBeNull();
  });

  it("bills the flat price up to the allowance and the seat price after", () => {
    expect([1, 5, 6, 8].map((n) => teamMonthlyUsd(P, n))).toEqual([50, 50, 70, 110]);
    expect(pricingFaq(P)[0].a).toContain("An eight-person team pays $110 a month.");
    expect(ordinal(6)).toBe("6th");
    expect(ordinal(21)).toBe("21st");
    expect(ordinal(12)).toBe("12th");
    expect(markupPercent("1.2")).toBe(20);
    expect(markupPercent("1.025")).toBe(2.5);
  });
});
