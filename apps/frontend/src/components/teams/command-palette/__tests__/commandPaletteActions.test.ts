// NAV_ACTIONS derives from the sidebar IA (COMPANY_SECTIONS) — the palette
// covers exactly what the sidebar shows, plus the roster and settings — and
// filterNavActions matches labels and keyword aliases.

import { describe, test, expect } from "vitest";
import { NAV_ACTIONS, filterNavActions } from "../commandPaletteActions";
import { COMPANY_SECTIONS } from "@/components/chat/companySections";

// NAV_ACTIONS is built from COMPANY_SECTIONS, so the palette offers
// exactly what the sidebar offers, by construction rather than by two lists
// kept in step by hand.
const OFFERED = COMPANY_SECTIONS;

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

  test("Activity, Library and Tasks carry the sidebar labels; Home is gone", () => {
    expect(byId["go-activity"]?.label).toBe("Activity");
    expect(byId["go-activity"]?.path).toBe("/chat?panel=activity");
    expect(byId["go-dashboard"]).toBeUndefined();
    expect(byId["go-artifacts"]?.label).toBe("Library");
    expect(byId["go-issues"]?.label).toBe("Tasks");
  });

  test("Mail opens its own panel, not a teams-* one", () => {
    expect(byId["go-mail"]?.path).toBe("/chat?panel=mail");
    expect(filterNavActions("meetings").map((a) => a.id)).toContain("go-mail");
  });

  test("the two non-sidebar extras survive: roster + settings", () => {
    expect(byId["go-agents"]?.path).toBe("/chat?panel=teams-agents");
    // Settings is its own route now, not a dock tab.
    expect(byId["go-settings"]?.path).toBe("/settings");
  });

  test("nothing beyond the offered set + the two extras", () => {
    const offeredCount = OFFERED.flatMap((s) => s.items).length;
    expect(NAV_ACTIONS).toHaveLength(offeredCount + 2);
  });

  test("the deleted Growth and Company tabs, and Accounts (a Settings section), are offered nowhere", () => {
    const declared = COMPANY_SECTIONS.flatMap((s) => s.items).map((i) => i.panel);
    expect(COMPANY_SECTIONS.map((s) => s.label)).not.toContain("Company");
    for (const key of ["publish", "seo", "ads", "leads", "identity", "domain-site", "compliance", "contracts", "accounts"]) {
      expect(declared).not.toContain(`teams-${key}`);
      expect(byId[`go-${key}`], `${key} should not be offered`).toBeUndefined();
    }
  });

  test("old labels survive as keyword aliases", () => {
    expect(filterNavActions("dashboard").map((a) => a.id)).toContain("go-activity");
    expect(filterNavActions("home").map((a) => a.id)).toContain("go-activity");
    expect(filterNavActions("artifacts").map((a) => a.id)).toContain("go-artifacts");
    expect(filterNavActions("issues").map((a) => a.id)).toContain("go-issues");
    expect(filterNavActions("approvals").map((a) => a.id)).toContain("go-activity");
    expect(filterNavActions("inbox").map((a) => a.id)).toEqual(["go-activity"]);
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
    const result = filterNavActions("AUTOMA");
    expect(result.map((a) => a.id)).toEqual(["go-routines"]);
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

  test("super-agent: no Work or Money; Settings, Business, Scheduled and Approvals open in the shell", () => {
    const actions = filterNavActions("", true);
    const byId = Object.fromEntries(actions.map((a) => [a.id, a.path]));
    for (const gone of ["go-money", "go-issues", "go-routines", "go-projects", "go-goals", "go-cases"]) expect(byId).not.toHaveProperty(gone);
    expect(byId["go-settings"]).toBe("/chat?panel=settings");
    expect(byId["go-business"]).toBe("/chat?panel=settings&section=business");
    expect(byId["go-scheduled"]).toBe("/chat?panel=activity&section=scheduled");
    expect(byId["go-approvals"]).toBe("/chat?panel=settings&section=approvals");
    expect(filterNavActions("revenue", true).map((a) => a.id)).toEqual(["go-business"]);
    // Approvals is an admin's row: a member is never offered it.
    expect(filterNavActions("", true, false).map((a) => a.id)).not.toContain("go-approvals");
  });
});
