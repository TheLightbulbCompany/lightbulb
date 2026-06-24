import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");

// The unified nav collapses the two sidebars (chat vs control) into one. The
// roster + CompanyNav + Gallery render unconditionally inside a single
// `.sidebar-scroll` region; the legacy ControlSidebar branch is gone.
describe("ChatLayout — unified single sidebar", () => {
  it("wraps the roster + CompanyNav + Gallery in a single .sidebar-scroll region", () => {
    expect(source).toMatch(/className="sidebar-scroll"/);
  });

  it("no longer imports or renders the legacy ControlSidebar", () => {
    expect(source).not.toMatch(/ControlSidebar/);
  });

  it("no longer branches on activeView for the sidebar body", () => {
    expect(source).not.toMatch(/activeView === "chat"/);
  });
});
