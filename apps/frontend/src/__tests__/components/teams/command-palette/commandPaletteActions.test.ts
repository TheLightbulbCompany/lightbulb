// Smoke tests for filterNavActions — empty query, label substring matching,
// keyword alias matching, and no-match returns empty.

import { describe, test, expect } from "vitest";
import { NAV_ACTIONS, filterNavActions } from "@/components/teams/command-palette/commandPaletteActions";

describe("filterNavActions", () => {
  test("empty query returns all 13 actions", () => {
    expect(filterNavActions("")).toHaveLength(13);
    expect(filterNavActions("   ")).toHaveLength(13);
  });

  test("substring matches label (case-insensitive)", () => {
    const result = filterNavActions("INBO");
    expect(result.map((a) => a.id)).toEqual(["go-inbox"]);
  });

  test("matches by keyword alias", () => {
    const result = filterNavActions("schedule");
    expect(result.map((a) => a.id)).toContain("go-routines");
    expect(filterNavActions("hierarchy").map((a) => a.id)).toEqual(["go-org-chart"]);
  });

  test("the retired Members action is gone", () => {
    expect(NAV_ACTIONS.map((a) => a.id)).not.toContain("go-members");
  });

  test("returns empty when no match", () => {
    expect(filterNavActions("xyzzy")).toHaveLength(0);
  });

  test("NAV_ACTIONS is exported with 13 entries", () => {
    expect(NAV_ACTIONS).toHaveLength(13);
  });
});
