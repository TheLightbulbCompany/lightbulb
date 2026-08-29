import { describe, expect, it } from "vitest";
import { LAUNCH_SCHEDULE, launchDatesFor } from "../launchSchedule";

describe("launch-schedule.json", () => {
  it("has the pot constants and both env keys", () => {
    expect(LAUNCH_SCHEDULE.pot_front_cents).toBe(100000);
    expect(LAUNCH_SCHEDULE.pot_per_entrant_cents).toBe(1000);
    expect(Object.keys(launchDatesFor("dev"))).toEqual(["drop_at", "competition_start_at"]);
    expect(Object.keys(launchDatesFor("prod"))).toEqual(["drop_at", "competition_start_at"]);
  });
  it("orders competition_start_at after drop_at wherever dates are set", () => {
    for (const env of ["dev", "prod"] as const) {
      const { drop_at, competition_start_at } = launchDatesFor(env);
      if (drop_at && competition_start_at) expect(Date.parse(competition_start_at)).toBeGreaterThan(Date.parse(drop_at));
    }
  });
});
