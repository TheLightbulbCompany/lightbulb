// Persisted dockview layout blob. Template: desktopPermissions.ts — version
// in the key AND the payload, strict validation, null-on-any-failure reads,
// swallow-on-failure writes (private browsing / quota → session just isn't
// persistent). NEVER call localStorage.clear() here: a key===null storage
// event is treated as a permissions event by the gateway listeners.
import type { SerializedDockview } from "dockview-react";
import { PANEL_COMPONENT_KEYS, ONE_SHOT_PARAMS } from "./panelRegistry";

const KEY_PREFIX = "isol8.workspace.layout.v1";

// Params that may live on a panel while it's open but must NOT survive into
// the persisted blob. initialTab is a navigation intent (the Get Started
// "Connect a channel" CTA), not view state — persisting it would land the
// inspector on that tab on EVERY restore, forever. Same for the inbox's
// deep-linked tab: the user's chosen tab lives in localStorage, and a
// persisted tab param would silently override it on every reload.
const PERSIST_ONLY_STRIP = ["initialTab", "tab"] as const;

interface StoredLayout {
  version: 1;
  savedAt: string;
  layout: SerializedDockview;
}

export function layoutStorageKey(scopeId: string): string {
  return `${KEY_PREFIX}:${scopeId}`;
}

// fromJSON THROWS (after clearing the dock) on an unregistered
// contentComponent — one stale key must not nuke the whole saved layout.
// Component names live only in the flat panels map (the grid tree references
// panel IDs), so remapping unknowns to the registered 'missing' placeholder
// is sufficient — no grid surgery needed. Also register 'unknown' in the
// components map: the deserializer defaults a missing contentComponent to
// the literal string 'unknown'.
export function sanitizeLayout(raw: SerializedDockview): SerializedDockview {
  const panels: SerializedDockview["panels"] = {};
  for (const [id, state] of Object.entries(raw.panels ?? {})) {
    const component =
      state.contentComponent && PANEL_COMPONENT_KEYS.has(state.contentComponent)
        ? state.contentComponent
        : "missing";
    let params = state.params;
    if (params) {
      params = { ...params };
      for (const oneShot of ONE_SHOT_PARAMS) delete params[oneShot];
      for (const key of PERSIST_ONLY_STRIP) delete params[key];
    }
    panels[id] = { ...state, contentComponent: component, params };
  }
  const clean: SerializedDockview = { grid: raw.grid, panels };
  if (raw.activeGroup !== undefined) clean.activeGroup = raw.activeGroup;
  // floatingGroups / popoutGroups / edgeGroups deliberately dropped:
  // disableFloatingGroups only blocks user-initiated floats — persisted
  // floating groups would still restore (and popouts window.open a 404).
  return clean;
}

export function readLayout(scopeId: string): SerializedDockview | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(layoutStorageKey(scopeId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredLayout>;
    if (parsed.version !== 1 || !parsed.layout || typeof parsed.layout !== "object") return null;
    return sanitizeLayout(parsed.layout);
  } catch {
    return null;
  }
}

export function writeLayout(scopeId: string, layout: SerializedDockview): void {
  if (typeof window === "undefined") return;
  try {
    const stored: StoredLayout = {
      version: 1,
      savedAt: new Date().toISOString(),
      layout: sanitizeLayout(layout),
    };
    window.localStorage.setItem(layoutStorageKey(scopeId), JSON.stringify(stored));
  } catch {
    // quota / privacy mode — non-persistent session, never crash
  }
}

export function clearLayout(scopeId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(layoutStorageKey(scopeId));
  } catch {
    // best effort
  }
}
