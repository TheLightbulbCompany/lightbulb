// Smoke tests for filterNavActions — empty query, label substring matching,
// keyword alias matching, and no-match returns empty.

import { describe, test, expect } from "vitest";
import { NAV_ACTIONS, filterNavActions } from "@/components/teams/command-palette/commandPaletteActions";

describe("filterNavActions", () => {
  test("empty query returns all 11 actions", () => {
    expect(filterNavActions("")).toHaveLength(11);
    expect(filterNavActions("   ")).toHaveLength(11);
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

  test("the retired Members / Skills / Costs actions are gone", () => {
    const ids = NAV_ACTIONS.map((a) => a.id);
    expect(ids).not.toContain("go-members"); // #1022
    expect(ids).not.toContain("go-skills"); // #1175 — Teams skills panel retired
    expect(ids).not.toContain("go-costs"); // #1236 — spend moved to the usage surfaces
  });

  test("returns empty when no match", () => {
    expect(filterNavActions("xyzzy")).toHaveLength(0);
  });

  test("NAV_ACTIONS is exported with 11 entries", () => {
    expect(NAV_ACTIONS).toHaveLength(11);
  });
});
