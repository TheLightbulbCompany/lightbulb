// Super-agent workspaces land on Home; a brand-new one stays in #general for onboarding; nothing named is overridden.
import { describe, expect, it, vi } from "vitest";
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({}), useUser: () => ({}) }));
import { isFreshWorkspace, landsOnHome } from "../MessagingWorkspaceContext";
import type { RoomSummary } from "@lightbulb/shared";

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

describe("an owner who works only in the Lightbulb DM (super-agent first run)", () => {
  const general = { roomId: "general", kind: "channel", isDefault: true } as RoomSummary;
  const dm = { roomId: "dm", kind: "dm" } as RoomSummary;
  it("lands in the DM while its first run is open, and on Home after a reload once they have answered", () => {
    // The backend clears `firstRun` with the owner's first answer (messaging.close_first_run).
    expect(isFreshWorkspace([general, { ...dm, firstRun: true }])).toBe(true);
    const fresh = isFreshWorkspace([general, dm]);
    expect(fresh).toBe(false);
    expect(landsOnHome({ ...base, fresh })).toBe(true);
  });
});
