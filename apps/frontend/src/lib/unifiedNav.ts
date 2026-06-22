// Runtime toggle for the unified-nav migration (Phase A). Opt in by
// visiting `?unifiedNav=1` (persists in this browser via localStorage);
// `?unifiedNav=0` opts out. TEMPORARY — delete when the migration ships.

export const UNIFIED_NAV_KEY = "isol8:unified-nav";

// The unified nav is a dev/preview-only escape hatch and must NOT be reachable
// on the production apex. The gate keys off the backend host: only the literal
// `//api.isol8.co` counts as prod. The `//` prefix is load-bearing — it
// excludes `//api-dev.isol8.co` and `//api-staging.isol8.co` (the `-dev`/
// `-staging` segment breaks the substring) and `//localhost`, so dev + Vercel
// previews keep full access. Evaluated at call time (not module scope) so the
// env is read live — important for tests stubbing NEXT_PUBLIC_API_URL.
function isProd(): boolean {
  return (process.env.NEXT_PUBLIC_API_URL ?? "").includes("//api.isol8.co");
}

export function readUnifiedNav(): boolean {
  if (isProd()) return false;
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(UNIFIED_NAV_KEY) === "1";
  } catch {
    return false;
  }
}

// "1" enables, "0" disables, anything else is ignored. Returns the new
// boolean when it acted, or null when the param was absent/invalid. On the prod
// apex this is always a null no-op so the localStorage key is never written.
export function applyUnifiedNavParam(param: string | null): boolean | null {
  if (isProd()) return null;
  if (param !== "1" && param !== "0") return null;
  if (typeof window === "undefined") return null;
  try {
    if (param === "1") {
      window.localStorage.setItem(UNIFIED_NAV_KEY, "1");
      return true;
    }
    window.localStorage.removeItem(UNIFIED_NAV_KEY);
    return false;
  } catch {
    return null;
  }
}

// Resolve the effective flag for a render: an explicit URL param overrides the
// stored value SYNCHRONOUSLY, so `?unifiedNav=0` opts out on the very first
// render (before the persistence effect runs) and `?unifiedNav=1` opts in; an
// absent/invalid param falls back to the stored value. On the prod apex it
// short-circuits to false before any param/stored consideration.
export function resolveUnifiedNav(param: string | null, stored: boolean): boolean {
  if (isProd()) return false;
  if (param === "1") return true;
  if (param === "0") return false;
  return stored;
}
