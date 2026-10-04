import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useIsDesktop } from "../useIsDesktop";

type Listener = (e: { matches: boolean }) => void;

function installMatchMedia(initialMatches: boolean) {
  const listeners = new Set<Listener>();
  const mql = {
    matches: initialMatches,
    media: "(min-width: 600px)",
    addEventListener: (_: string, cb: Listener) => listeners.add(cb),
    removeEventListener: (_: string, cb: Listener) => listeners.delete(cb),
  };
  vi.stubGlobal("matchMedia", vi.fn().mockReturnValue(mql));
  return {
    fire(matches: boolean) {
      mql.matches = matches;
      listeners.forEach((cb) => cb({ matches }));
    },
  };
}

beforeEach(() => vi.unstubAllGlobals());

describe("useIsDesktop", () => {
  it("reflects the media query and reacts to changes", () => {
    const mm = installMatchMedia(true);
    const { result } = renderHook(() => useIsDesktop());
    expect(result.current).toBe(true);
    act(() => mm.fire(false));
    expect(result.current).toBe(false);
  });

  it("queries exactly (min-width: 600px)", () => {
    installMatchMedia(false);
    renderHook(() => useIsDesktop());
    expect(window.matchMedia).toHaveBeenCalledWith("(min-width: 600px)");
  });

  // The workspace sidebar imports ChatLayout.css, so a CSS block that hides
  // .cream-sidebar at a width where the hook still says desktop blanks it.
  it("is the exact complement of ChatLayout.css's mobile block", () => {
    const css = readFileSync(path.resolve(__dirname, "../../components/chat/ChatLayout.css"), "utf8");
    expect(css).toMatch(/@media \(max-width: 599px\) \{\s*\.app-shell \{/);
    expect(css).not.toContain("max-width: 859px");
  });
});
