import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _resetControllerForTests, attachShell, getWorkspaceSnapshot, openPanel } from "../workspaceController";
import { ideasFirstOpenKey, openIdeasOnFirstUse } from "../ideasFirstOpen";

const key = ideasFirstOpenKey("u1", "org-1");
const wide = (matches: boolean) => vi.stubGlobal("matchMedia", (query: string) => ({ matches, media: query }));

beforeEach(() => {
  _resetControllerForTests();
  window.localStorage.clear();
  attachShell(() => undefined);
  wide(true);
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("Ideas on first use", () => {
  it("opens the Ideas pane the first time Chat opens, once per person per workspace", () => {
    expect(openIdeasOnFirstUse(key)).toBe(true);
    expect(getWorkspaceSnapshot().pane?.panelId).toBe("ideas");
    _resetControllerForTests();
    attachShell(() => undefined);
    expect(openIdeasOnFirstUse(key)).toBe(false);
    expect(getWorkspaceSnapshot().pane).toBeNull();
    // Another workspace is a first time again.
    expect(openIdeasOnFirstUse(ideasFirstOpenKey("u1", "org-2"))).toBe(true);
  });

  it("does not open on a phone-width screen, nor over another pane, nor outside Chat", () => {
    wide(false);
    expect(openIdeasOnFirstUse(key)).toBe(false);
    wide(true);
    openPanel("browser");
    expect(openIdeasOnFirstUse(key)).toBe(false);
    expect(getWorkspaceSnapshot().pane?.panelId).toBe("browser");
    expect(window.localStorage.getItem(key)).toBeNull();
  });

  it("never opens by itself when storage refuses the flag", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(openIdeasOnFirstUse(key)).toBe(false);
    expect(getWorkspaceSnapshot().pane).toBeNull();
  });
});
