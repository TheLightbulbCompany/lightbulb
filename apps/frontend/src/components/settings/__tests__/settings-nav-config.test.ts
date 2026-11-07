import { describe, it, expect } from "vitest";
import { SETTINGS_NAV_SECTIONS } from "../settings-nav-config";

const ids = SETTINGS_NAV_SECTIONS.flatMap((section) => section.items.map((item) => item.id));

describe("SETTINGS_NAV_SECTIONS", () => {
  // The one Settings page (product simplification, 2026-10-14): Control's
  // panels, Team Settings and Accounts all live here, grouped by intent.
  it("groups every row as the owner approved", () => {
    expect(
      SETTINGS_NAV_SECTIONS.map((s) => [s.label, s.items.map((i) => i.id)]),
    ).toEqual([
      ["You", ["profile"]],
      ["Workspace", ["workspace", "members", "approvals"]],
      ["Connect", ["connections"]],
      ["Plan", ["billing", "usage", "business", "providers"]],
      ["Developer", ["skills", "mcp", "local-mcp", "desktop"]],
    ]);
  });

  // Channel wiring, the read-only channel-status engine room, and Dreams/Memory
  // have no Hermes equivalent, so the rows are GONE rather than gated.
  it("offers no row for a surface the runtime cannot serve", () => {
    expect(ids).not.toContain("channels");
    expect(ids).not.toContain("channel-status");
    expect(ids).not.toContain("dreams");
  });

  it("is all in-page tabs: no row navigates away", () => {
    expect(SETTINGS_NAV_SECTIONS.flatMap((s) => s.items).filter((i) => i.href)).toEqual([]);
  });

  it("never lists a row twice", () => {
    expect(new Set(ids).size).toBe(ids.length);
  });
});
