import { describe, expect, it } from "vitest";

import { PRICING_FAQ, teamMonthlyUsd } from "../pricing";

describe("one plan pricing", () => {
  it("bills $50 up to five people and $20 for each person from the sixth", () => {
    expect(teamMonthlyUsd(1)).toBe(50);
    expect(teamMonthlyUsd(5)).toBe(50);
    expect(teamMonthlyUsd(6)).toBe(70);
    expect(teamMonthlyUsd(8)).toBe(110);
  });

  it("quotes the eight-person example from the same math", () => {
    expect(PRICING_FAQ[0].a).toContain("An eight-person team pays $110 a month.");
  });
});
