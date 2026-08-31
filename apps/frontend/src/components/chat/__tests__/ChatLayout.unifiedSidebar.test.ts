import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");

// The unified nav collapses the two sidebars (chat vs control) into one. The
// roster + CompanyNav + Gallery render unconditionally inside a single
// `.sidebar-scroll` region; the legacy control-sidebar branch is gone.
describe("ChatLayout — unified single sidebar", () => {
  it("wraps the roster + CompanyNav + Gallery in a single .sidebar-scroll region", () => {
    expect(source).toMatch(/className="sidebar-scroll"/);
  });

  it("no longer imports anything from the control components (the legacy control-sidebar branch)", () => {
    expect(source).not.toMatch(/components\/control\//);
  });

  it("no longer branches on activeView for the sidebar body", () => {
    expect(source).not.toMatch(/activeView === "chat"/);
  });
});
