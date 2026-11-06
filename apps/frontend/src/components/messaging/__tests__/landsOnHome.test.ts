// Super-agent workspaces land on Home; a brand-new one stays in #general for onboarding; nothing named is overridden.
import { describe, expect, it, vi } from "vitest";
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({}), useUser: () => ({}) }));
import { landsOnHome } from "../MessagingWorkspaceContext";

const base = { superAgent: true, fresh: false as boolean | null, search: "", snapshot: { ready: true, place: "chat" as const, pane: null }, picked: false };

describe("landsOnHome", () => {
  it("a returning super-agent workspace on a bare URL lands on Home", () => {
    expect(landsOnHome(base)).toBe(true);
  });
  it.each([
    ["a brand-new workspace (#general onboarding)", { fresh: true }],
    ["fresh-or-returning not known yet", { fresh: null }],
    ["the switch off", { superAgent: false }],
    ["a URL naming a place", { search: "?panel=activity" }],
    ["a room permalink", { search: "?panel=chat&room=r1" }],
    ["a place already open", { snapshot: { ready: true, place: "work" as const, pane: null } }],
    ["a pick already made", { picked: true }],
  ])("stays put for %s", (_label, change) => {
    expect(landsOnHome({ ...base, ...change } as typeof base)).toBe(false);
  });
});
