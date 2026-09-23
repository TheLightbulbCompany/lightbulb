import { describe, it, expect } from "vitest";
import { SETTINGS_NAV_SECTIONS } from "../settings-nav-config";

const ids = SETTINGS_NAV_SECTIONS.flatMap((section) => section.items.map((item) => item.id));

describe("SETTINGS_NAV_SECTIONS", () => {
  // Channel wiring, the read-only channel-status engine room, and Dreams/Memory
  // have no Hermes equivalent, so the rows are GONE rather than gated.
  it("offers no row for a surface the runtime cannot serve", () => {
    expect(ids).not.toContain("channels");
    expect(ids).not.toContain("channel-status");
    expect(ids).not.toContain("dreams");
  });

  it("keeps the sibling WIRING and ENGINE ROOM rows", () => {
    expect(ids).toContain("mcp");
    expect(ids).toContain("skills");
  });

  it("never lists a row twice", () => {
    expect(new Set(ids).size).toBe(ids.length);
  });
});
