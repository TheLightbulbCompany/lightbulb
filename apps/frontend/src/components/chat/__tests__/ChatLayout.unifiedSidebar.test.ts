import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");

// Phase D: the unified nav collapses the two sidebars (chat vs control) into
// one. Implemented by widening the sidebar-body branch condition so the
// chat-branch body (roster + CompanyNav) renders whenever unifiedNav is on —
// not only in chat view — and the separate control-branch sidebar (with
// ControlSidebar) is reached only on the flag-off path.
describe("ChatLayout — unified single sidebar (Phase D)", () => {
  it("renders the unified sidebar body whenever unifiedNav is on, not just chat view", () => {
    expect(source).toMatch(/unifiedNav\s*\|\|\s*activeView === "chat"\s*\?/);
  });

  it("keeps ControlSidebar (flag-off control branch only — redundant under unifiedNav)", () => {
    // Under unifiedNav the chat-branch body covers the sidebar, so ControlSidebar
    // is redundant in the unified path. It stays imported + rendered for the
    // flag-off control view (the ternary's else arm).
    expect(source).toMatch(/<ControlSidebar\b/);
  });

  it("flag-off condition is byte-equivalent (false || X === X)", () => {
    // `unifiedNav || activeView === "chat"` reduces to `activeView === "chat"`
    // when the flag is off, so the flag-off sidebar is unchanged. (The behavior
    // is exercised by the other ChatLayout suites, which run flag-off.)
    expect(source).toMatch(/unifiedNav \|\| activeView === "chat"/);
  });
});
