import { afterEach, describe, expect, it, vi } from "vitest";

import { VOICE_PLAN_NEUTRAL, getPricing, pricingFaq, voicePlanLine } from "../pricing";

const json = (body: unknown) => new Response(JSON.stringify(body));
const API = {
  voice_plan_monthly_usd: 250,
  voice_included_minutes: 1500,
  voice_overage_usd_per_minute: 0.35,
};
const P = {
  voiceMonthlyUsd: 250,
  voiceIncludedMinutes: 1500,
  voiceOverageUsd: 0.35,
};

afterEach(() => vi.unstubAllGlobals());

describe("pricing from the billing API", () => {
  it("reads the prices from the billing API", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(API)));
    expect(await getPricing()).toEqual(P);
  });

  it("reads a missing voice price as null", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ ...API, voice_plan_monthly_usd: null })));
    expect(await getPricing()).toEqual({ ...P, voiceMonthlyUsd: null });
  });

  it("returns null, never a guessed number, when the API is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("down", { status: 503 })));
    expect(await getPricing()).toBeNull();
  });

  it("states the voice plan from the API numbers, and stays neutral without them", () => {
    expect(voicePlanLine(P)).toBe("$250 a month includes 1,500 minutes, then $0.35 a minute.");
    expect(voicePlanLine({ ...P, voiceOverageUsd: null })).toBeNull();
    expect(voicePlanLine(null)).toBeNull();
    const faq = pricingFaq(P).map(({ a }) => a).join(" ");
    expect(faq).toContain("$250 a month includes 1,500 minutes");
    expect(faq).toContain("keep answering at $0.35 a minute");
    const neutral = pricingFaq(null).map(({ a }) => a).join(" ");
    expect(neutral).toContain(VOICE_PLAN_NEUTRAL);
    expect(neutral).toContain("the per-minute rate");
    expect(neutral).not.toMatch(/\$\d+ a month|\$0\./);
  });
});
