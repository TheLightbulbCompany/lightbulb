// Runtime toggle for the unified nav. The unified `/chat` shell is now the
// DEFAULT everywhere — no `?unifiedNav=1` opt-in is needed. A trivial opt-OUT
// escape hatch remains: `?unifiedNav=0` falls back to the legacy nav for this
// browser (persisted in localStorage), and `?unifiedNav=1` clears the opt-out.
// Keeping the opt-out makes this cleanly reversible while `/teams` and the
// legacy nav are still around (both retired in a later, separate step).

export const UNIFIED_NAV_KEY = "isol8:unified-nav";

// Whether the unified nav is enabled in this browser. Default ON: the only
// stored value that disables it is the explicit opt-out marker `"0"`.
export function readUnifiedNav(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem(UNIFIED_NAV_KEY) !== "0";
  } catch {
    return true;
  }
}

// "0" persists the opt-out (legacy nav), "1" clears it (back to the default
// unified nav), anything else is ignored. Returns the new boolean when it
// acted, or null when the param was absent/invalid.
export function applyUnifiedNavParam(param: string | null): boolean | null {
  if (param !== "1" && param !== "0") return null;
  if (typeof window === "undefined") return null;
  try {
    if (param === "0") {
      window.localStorage.setItem(UNIFIED_NAV_KEY, "0");
      return false;
    }
    window.localStorage.removeItem(UNIFIED_NAV_KEY);
    return true;
  } catch {
    return null;
  }
}

// Resolve the effective flag for a render: an explicit URL param overrides the
// stored value SYNCHRONOUSLY, so `?unifiedNav=0` opts out on the very first
// render (before the persistence effect runs) and `?unifiedNav=1` opts back in;
// an absent/invalid param falls back to the stored value (which defaults ON).
export function resolveUnifiedNav(param: string | null, stored: boolean): boolean {
  if (param === "1") return true;
  if (param === "0") return false;
  return stored;
}
