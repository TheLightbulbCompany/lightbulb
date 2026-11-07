// navActions derives from the sidebar IA (COMPANY_SECTIONS) minus what moved into the shell, and
// filterNavActions matches labels and keyword aliases.

import { describe, test, expect } from "vitest";
import { navActions, filterNavActions } from "../commandPaletteActions";
import { COMPANY_SECTIONS } from "@/components/chat/companySections";

const byId = Object.fromEntries(navActions().map((a) => [a.id, a]));

describe("navActions", () => {

  test("Mail opens its own panel, not a teams-* one", () => {
    expect(byId["go-mail"]?.path).toBe("/chat?panel=mail");
    expect(filterNavActions("meetings").map((a) => a.id)).toContain("go-mail");
  });

  test("the deleted Growth and Company tabs, and Accounts (a Settings section), are offered nowhere", () => {
    const declared = COMPANY_SECTIONS.flatMap((s) => s.items).map((i) => i.panel);
    expect(COMPANY_SECTIONS.map((s) => s.label)).not.toContain("Company");
    for (const key of ["publish", "seo", "ads", "leads", "identity", "domain-site", "compliance", "contracts", "accounts"]) {
      expect(declared).not.toContain(`teams-${key}`);
      expect(byId[`go-${key}`], `${key} should not be offered`).toBeUndefined();
    }
  });

  test("the absorbed Spend/Payments/etc. search words find Business", () => {
    expect(filterNavActions("billing").map((a) => a.id)).toContain("go-business");
  });
});

describe("filterNavActions", () => {
  test("empty query returns every action", () => {
    expect(filterNavActions("")).toHaveLength(navActions().length);
    expect(filterNavActions("   ")).toHaveLength(navActions().length);
  });

  test("substring matches label (case-insensitive)", () => {
    const result = filterNavActions("SCHEDUL");
    expect(result.map((a) => a.id)).toEqual(["go-scheduled"]);
  });

  test("matches by keyword alias", () => {
    const result = filterNavActions("cron");
    expect(result.map((a) => a.id)).toContain("go-scheduled");
    // The org chart is a view of the Agents panel now, so its words land there.
    expect(filterNavActions("hierarchy").map((a) => a.id)).toEqual(["go-agents"]);
  });

  test("the retired Members / Skills / Costs actions are gone", () => {
    const ids = navActions().map((a) => a.id);
    expect(ids).not.toContain("go-members"); // #1022
    expect(ids).not.toContain("go-skills"); // #1175 — Teams skills panel retired
    expect(ids).not.toContain("go-costs"); // #1236 — spend moved to the usage surfaces
  });

  test("returns empty when no match", () => {
    expect(filterNavActions("xyzzy")).toHaveLength(0);
  });

  test("no Work or Money; Settings, Business, Scheduled and Approvals open in the shell", () => {
    const actions = filterNavActions("");
    const byId = Object.fromEntries(actions.map((a) => [a.id, a.path]));
    for (const gone of ["go-money", "go-issues", "go-routines", "go-projects", "go-goals", "go-cases"]) expect(byId).not.toHaveProperty(gone);
    expect(byId["go-settings"]).toBe("/chat?panel=settings");
    expect(byId["go-business"]).toBe("/chat?panel=settings&section=business");
    expect(byId["go-scheduled"]).toBe("/chat?panel=activity&section=scheduled");
    expect(byId["go-approvals"]).toBe("/chat?panel=settings&section=approvals");
    expect(filterNavActions("revenue").map((a) => a.id)).toEqual(["go-business"]);
    // Approvals is an admin's row: a member is never offered it.
    expect(filterNavActions("", false).map((a) => a.id)).not.toContain("go-approvals");
  });
});
