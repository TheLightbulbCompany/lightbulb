// NAV_ACTIONS derives from the sidebar IA (COMPANY_SECTIONS) — the palette
// covers exactly what the sidebar shows, plus the roster and settings — and
// filterNavActions matches labels and keyword aliases.

import { describe, test, expect } from "vitest";
import { NAV_ACTIONS, filterNavActions } from "../commandPaletteActions";
import { COMPANY_SECTIONS, visibleCompanySections } from "@/components/chat/companySections";

// NAV_ACTIONS is built from `visibleCompanySections`, so the palette offers
// exactly what the rail offers — a shell hidden from one is hidden from both,
// by construction rather than by two lists kept in step by hand.
const OFFERED = visibleCompanySections({ provisioningEnabled: true });

const byId = Object.fromEntries(NAV_ACTIONS.map((a) => [a.id, a]));

describe("NAV_ACTIONS derives from the sidebar IA", () => {
  test("every offered sidebar item appears with its section-derived path", () => {
    for (const section of OFFERED) {
      for (const item of section.items) {
        const key = item.panel.replace(/^teams-/, "");
        const action = byId[`go-${key}`];
        expect(action, `missing palette action for ${item.panel}`).toBeDefined();
        expect(action?.label).toBe(item.label);
        expect(action?.path).toBe(item.panel.startsWith("teams-") ? `/chat?panel=teams-${key}` : `/chat?panel=${item.panel}`);
      }
    }
  });

  test("Home, Library and Tasks carry the sidebar labels", () => {
    expect(byId["go-dashboard"]?.label).toBe("Home");
    expect(byId["go-artifacts"]?.label).toBe("Library");
    expect(byId["go-issues"]?.label).toBe("Tasks");
  });

  test("Mail opens its own panel, not a teams-* one", () => {
    expect(byId["go-mail"]?.path).toBe("/chat?panel=mail");
    expect(filterNavActions("meetings").map((a) => a.id)).toContain("go-mail");
  });

  test("the two non-sidebar extras survive: roster + settings", () => {
    expect(byId["go-agents"]?.path).toBe("/chat?panel=teams-agents");
    expect(byId["go-settings"]?.path).toBe("/chat?panel=teams-settings");
  });

  test("nothing beyond the offered set + the two extras", () => {
    const offeredCount = OFFERED.flatMap((s) => s.items).length;
    expect(NAV_ACTIONS).toHaveLength(offeredCount + 2);
  });

  test("a shell is not offered by the palette either", () => {
    // The rail stops offering these; ⌘K must not become the back door that
    // walks an owner into an empty frame.
    for (const panel of [
      "teams-publish",
      "teams-seo",
      "teams-ads",
      "teams-identity",
      "teams-compliance",
      "teams-contracts",
    ]) {
      const key = panel.replace(/^teams-/, "");
      expect(byId[`go-${key}`], `${panel} should not be offered`).toBeUndefined();
      expect(filterNavActions(key).map((a) => a.id)).not.toContain(`go-${key}`);
    }
    // ...but they are still declared in the IA, so the keys stay real.
    const declared = COMPANY_SECTIONS.flatMap((s) => s.items).map((i) => i.panel);
    expect(declared).toContain("teams-publish");
  });

  test("old labels survive as keyword aliases", () => {
    expect(filterNavActions("dashboard").map((a) => a.id)).toContain("go-dashboard");
    expect(filterNavActions("artifacts").map((a) => a.id)).toContain("go-artifacts");
    expect(filterNavActions("issues").map((a) => a.id)).toContain("go-issues");
    expect(filterNavActions("approvals").map((a) => a.id)).toContain("go-inbox");
  });

  test("the absorbed Spend/Payments/etc. search words still find Money", () => {
    expect(filterNavActions("billing").map((a) => a.id)).toContain("go-money");
  });
});

describe("filterNavActions", () => {
  test("empty query returns every action", () => {
    expect(filterNavActions("")).toHaveLength(NAV_ACTIONS.length);
    expect(filterNavActions("   ")).toHaveLength(NAV_ACTIONS.length);
  });

  test("substring matches label (case-insensitive)", () => {
    const result = filterNavActions("INBO");
    expect(result.map((a) => a.id)).toEqual(["go-inbox"]);
  });

  test("matches by keyword alias", () => {
    const result = filterNavActions("schedule");
    expect(result.map((a) => a.id)).toContain("go-routines");
    // The org chart is a view of the Agents panel now, so its words land there.
    expect(filterNavActions("hierarchy").map((a) => a.id)).toEqual(["go-agents"]);
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
});
