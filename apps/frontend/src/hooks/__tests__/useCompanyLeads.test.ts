import { describe, it, expect } from "vitest";
import { ApiError } from "@lightbulb/shared";
import { leadsView, type LeadsEnvelope } from "../useCompanyLeads";

const env = (over: Partial<LeadsEnvelope> = {}): LeadsEnvelope => ({
  connected: true,
  data: [],
  total: 0,
  page: 1,
  has_more: false,
  ...over,
});

describe("leadsView", () => {
  it("is idle until the owner has asked for something — no auto-fired search", () => {
    expect(leadsView("", undefined, undefined, false, 0)).toEqual({ kind: "idle" });
  });

  it("reads 'no key' off the flag, never off an empty list", () => {
    expect(leadsView("growth", env({ connected: false }), undefined, false, 0)).toEqual({
      kind: "unbound",
    });
    expect(leadsView("growth", env(), undefined, false, 0)).toEqual({ kind: "zero" });
  });

  it("names a rejected key as its own state, because the fix is re-pasting, not waiting", () => {
    const rejected = new ApiError(502, { detail: { code: "apollo_key_rejected" } });
    expect(leadsView("growth", undefined, rejected, false, 0)).toEqual({ kind: "key_rejected" });
    const outage = new ApiError(502, { detail: { code: "apollo_unavailable" } });
    expect(leadsView("growth", undefined, outage, false, 0)).toEqual({ kind: "unavailable" });
  });

  it("separates a non-admin 403 from an outage", () => {
    expect(leadsView("growth", undefined, new ApiError(403, {}), false, 0)).toEqual({
      kind: "forbidden",
    });
  });

  it("never lets a later page take away rows already shown", () => {
    // Same lesson as Money: an accumulated table outranks an in-flight
    // fetch, an empty last page, and a 502 on page 3.
    expect(leadsView("growth", undefined, undefined, true, 25)).toEqual({ kind: "rows" });
    expect(leadsView("growth", env(), undefined, false, 25)).toEqual({ kind: "rows" });
    const outage = new ApiError(502, { detail: { code: "apollo_unavailable" } });
    expect(leadsView("growth", undefined, outage, false, 25)).toEqual({ kind: "rows" });
    const rejected = new ApiError(502, { detail: { code: "apollo_key_rejected" } });
    expect(leadsView("growth", undefined, rejected, false, 25)).toEqual({ kind: "rows" });
    // The one exception: a 403 is permanent, and page 1 would have refused.
    expect(leadsView("growth", undefined, new ApiError(403, {}), false, 25)).toEqual({
      kind: "forbidden",
    });
  });
});
