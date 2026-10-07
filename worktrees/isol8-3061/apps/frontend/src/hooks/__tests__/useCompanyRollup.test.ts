import { describe, it, expect } from "vitest";
import { dayOffset, rollupWindow } from "@/hooks/useCompanyRollup";

/**
 * The days below are hand-computed in UTC, so they pin the helpers' semantics
 * rather than the runner's. On a host EAST of Greenwich a local-midnight
 * implementation fails every case; on a UTC or western host it would slip
 * through — verify with `TZ=Pacific/Kiritimati npx vitest run <this file>`.
 *
 * ponytail: the zone cannot be pinned from inside the file. Vitest freezes the
 * worker's zone at process start, so `process.env.TZ = …`, `vi.stubEnv("TZ", …)`
 * and `globalThis.process.env.TZ` are all no-ops for `Date` (measured: offset
 * unchanged). Only `TZ=` on the vitest process itself takes. Making that
 * standing coverage means prefixing the frontend `test` script, which pins the
 * zone for all ~380 test files — a repo-wide call, not this file's.
 */
describe("rollup window arithmetic (UTC, never host-local)", () => {
  it("offsets a YYYY-MM-DD day in UTC across a month boundary", () => {
    expect(dayOffset("2026-09-01", -1)).toBe("2026-08-31");
    expect(dayOffset("2026-03-01", -1)).toBe("2026-02-28");
    expect(dayOffset("2028-03-01", -1)).toBe("2028-02-29"); // leap day
    expect(dayOffset("2026-08-30", 2)).toBe("2026-09-01");
  });

  it("asks for twice the span so a previous-period comparison is possible", () => {
    // 30d window ending 2026-09-01: current = Aug 3..Sep 1, previous = Jul 4..Aug 2.
    const w = rollupWindow(30, new Date("2026-09-01T23:59:00Z"));
    expect(w).toEqual({ start: "2026-07-04", windowStart: "2026-08-03", end: "2026-09-01" });
  });

  it("stays under the route's 366-day cap at the widest span", () => {
    const w = rollupWindow(90, new Date("2026-09-01T00:00:00Z"));
    expect(w.start).toBe("2026-03-06"); // 180 days inclusive
  });
});
