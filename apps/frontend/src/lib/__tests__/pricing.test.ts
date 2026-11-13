import { afterEach, describe, expect, it, vi } from "vitest";

import { getPricing, pricingFaq, voicePlanLine } from "../pricing";

const json = (body: unknown) => new Response(JSON.stringify(body));
const API = { workspace_price_usd: 50, included_members: 5, seat_price_usd: 20, trial_days: 30, managed_markup: "1.2", front_desk_price_usd: 250, voice_plan_monthly_usd: 250, voice_included_minutes: 1500, voice_overage_usd_per_minute: 0.35 };
const P = { teamPriceUsd: 50, includedMembers: 5, seatPriceUsd: 20, trialDays: 30, managedMarkup: "1.2", frontDeskPriceUsd: 250, voiceMonthlyUsd: 250, voiceIncludedMinutes: 1500, voiceOverageUsd: 0.35 };

afterEach(() => vi.unstubAllGlobals());

describe("one plan pricing", () => {
  it("reads the price from the billing API", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(API)));
    expect(await getPricing()).toEqual(P);
  });

  it("keeps the other prices when the API has no front desk price yet", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ ...API, front_desk_price_usd: undefined })));
    expect(await getPricing()).toEqual({ ...P, frontDeskPriceUsd: null });
  });

  it("returns null, never a guessed number, when the API is unreachable or malformed", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("down", { status: 503 })));
    expect(await getPricing()).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => json({ ...API, managed_markup: null })));
    expect(await getPricing()).toBeNull();
  });

  it("states the voice plan from the API numbers, neutral without them", () => {
    expect(voicePlanLine(P)).toBe("$250 a month includes 1,500 call minutes, then $0.35 a minute.");
    expect(voicePlanLine({ ...P, voiceMonthlyUsd: null })).toBeNull();
    expect(pricingFaq(P)[0].a).toContain("$250 a month includes 1,500 call minutes");
    expect(pricingFaq({ ...P, voiceOverageUsd: null })[0].a).toContain("per-minute rate");
  });
});
