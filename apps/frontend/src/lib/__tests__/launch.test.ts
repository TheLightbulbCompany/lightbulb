import { describe, expect, it } from "vitest";
import { formatMoney, formatPct } from "../launch";

describe("launch formatters", () => {
  it("formats whole-currency money", () => {
    expect(formatMoney(123456, "usd")).toBe("$1,235");
    expect(formatMoney(-2000, "usd")).toBe("-$20");
    expect(formatMoney(0, "eur")).toBe("€0");
  });
  it("formats percent with sign, and 'new' for no baseline", () => {
    expect(formatPct(0.25)).toBe("+25%");
    expect(formatPct(-0.04)).toBe("−4%");
    expect(formatPct(null)).toBe("new");
  });
});
