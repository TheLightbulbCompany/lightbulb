// The URL is a ONE-WAY retained command channel (spec §6): inbound ?panel=…
// is a navigation intent the shell executes as open-or-focus; outbound, only
// explicit navigations write it. Closing the pane only ever CLEARS it, in place.
import {
  describePanel,
  durableParamsFor,
  opensInPane,
  ONE_SHOT_PARAMS,
  NON_COMMAND_PARAMS,
  type PanelDescriptor,
} from "./panelRegistry";

/** The right pane rides along a place's URL as `pane=<key>` plus `pane.<param>=…`, so a
 *  reload restores both slots. With Chat as the place, the pane is the plain `panel=` form. */
export const PANE_PARAM = "pane";
const PANE_PREFIX = "pane.";

export interface PaneCommand {
  key: string;
  params: Record<string, string>;
  descriptor: PanelDescriptor;
}

export interface WorkspaceCommand {
  descriptor: PanelDescriptor;
  oneShots: Record<string, string>;
  pane?: PaneCommand;
}

/** Splits the pane part (`pane`, `pane.*`) off a URL param bag; the bag keeps the rest. */
export function takePane(all: Record<string, string>): PaneCommand | undefined {
  const key = all[PANE_PARAM];
  const params: Record<string, string> = {};
  for (const k of Object.keys(all)) {
    if (k === PANE_PARAM) delete all[k];
    else if (k.startsWith(PANE_PREFIX)) { params[k.slice(PANE_PREFIX.length)] = all[k]; delete all[k]; }
  }
  const descriptor = key ? describePanel(key, params) : null;
  return key && descriptor ? { key, params, descriptor } : undefined;
}

/** `target` (a Chat view: ?panel=chat&room=…) keeps the pane the current URL shows beside it:
 *  its `pane` part, or a plain `?panel=<pane key>` (the pane with Chat as the place). */
export function withPane(target: string, currentSearch: string): string {
  const current = toParams(currentSearch);
  const out = toParams(target);
  if (current.has(PANE_PARAM)) {
    current.forEach((v, k) => { if (k === PANE_PARAM || k.startsWith(PANE_PREFIX)) out.set(k, v); });
    return out.toString();
  }
  const command = parseCommand(currentSearch);
  const key = current.get("panel");
  if (!command || !key || !opensInPane(command.descriptor)) return target;
  const params: Record<string, string> = {};
  const skip = new Set<string>([...ONE_SHOT_PARAMS, ...NON_COMMAND_PARAMS, "panel"]);
  current.forEach((v, k) => { if (!skip.has(k)) params[k] = v; });
  appendPane(out, key, params);
  return out.toString();
}

/** `panel=<main>&…&pane=<key>&pane.<k>=…` */
export function appendPane(sp: URLSearchParams, key: string, params: Record<string, string>): void {
  sp.set(PANE_PARAM, key);
  for (const [k, v] of Object.entries(params)) sp.set(`${PANE_PREFIX}${k}`, v);
}

function toParams(search: string): URLSearchParams {
  return new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
}

export function parseCommand(search: string): WorkspaceCommand | null {
  const params = toParams(search);
  const all: Record<string, string> = {};
  params.forEach((v, k) => {
    all[k] = v;
  });

  const oneShots: Record<string, string> = {};
  for (const k of ONE_SHOT_PARAMS) {
    if (all[k] !== undefined) {
      oneShots[k] = all[k];
      delete all[k];
    }
  }
  for (const k of NON_COMMAND_PARAMS) delete all[k];
  const pane = takePane(all);

  const panelKey = all.panel;
  delete all.panel;

  if (!panelKey) {
    // Bare ?agent= (marketplace deploy-success) = open that agent's chat.
    if (all.agent) {
      const descriptor = describePanel("chat", { agent: all.agent });
      return descriptor ? { descriptor, oneShots, ...(pane ? { pane } : {}) } : null;
    }
    // Bare ?room= (the meeting-notes mail links this way) = open that room.
    if (all.room) {
      const descriptor = describePanel("chat", { room: all.room, ...(all.message ? { message: all.message } : {}) });
      return descriptor ? { descriptor, oneShots, ...(pane ? { pane } : {}) } : null;
    }
    return null;
  }

  const descriptor = describePanel(panelKey, all);
  return descriptor ? { descriptor, oneShots, ...(pane ? { pane } : {}) } : null;
}

// Stable identity of "what command does this URL carry" — the reconciler's
// lastApplied guard compares signatures, so billing-return strips and other
// non-command churn never re-execute a command.
export function commandSignature(search: string): string {
  const params = toParams(search);
  const kept: string[] = [];
  const skip = new Set<string>(NON_COMMAND_PARAMS);
  [...params.keys()].sort().forEach((k) => {
    if (!skip.has(k)) kept.push(`${k}=${params.get(k)}`);
  });
  return kept.join("&");
}

export function stripOneShots(search: string): string | null {
  const params = toParams(search);
  let changed = false;
  for (const k of ONE_SHOT_PARAMS) {
    if (params.has(k)) {
      params.delete(k);
      changed = true;
    }
  }
  return changed ? params.toString() : null;
}

export function buildPanelUrl(panelKey: string, params: Record<string, string>): string {
  const sp = new URLSearchParams();
  sp.set("panel", panelKey);
  for (const [k, v] of Object.entries(params)) sp.set(k, v);
  return `/chat?${sp.toString()}`;
}

// Closing the URL-named panel canonicalizes the URL in place (replace, never
// push) so a refresh can't resurrect the panel the user just closed. Returns
// null when the URL names a different panel — or when clearing would leave
// the search string unchanged, so the caller never issues an identical
// replaceState.
export function clearPanelFromUrl(search: string, closedPanelId: string): string | null {
  const command = parseCommand(search);
  if (command?.pane?.descriptor.panelId === closedPanelId) {
    const params = toParams(search);
    [...params.keys()].forEach((k) => { if (k === PANE_PARAM || k.startsWith(PANE_PREFIX)) params.delete(k); });
    return params.toString();
  }
  if (!command || command.descriptor.panelId !== closedPanelId) return null;
  const params = toParams(search);
  const before = params.toString();
  for (const k of durableParamsFor(closedPanelId)) params.delete(k);
  for (const k of ONE_SHOT_PARAMS) params.delete(k);
  const result = params.toString();
  return result === before ? null : result;
}

// Dismissing a durable panel param (the Tasks agent-filter chip) must also
// strip that param from the URL when the CURRENT URL command names the same
// panel — otherwise a refresh / Back re-executes the command and resurrects
// the filter the user explicitly dismissed. Returns the new search string,
// or null when the URL names a different panel or doesn't carry the param
// (no identical replaceState may be issued).
export function clearDurableParamFromUrl(
  search: string,
  panelId: string,
  urlParamKey: string,
): string | null {
  const command = parseCommand(search);
  if (!command || command.descriptor.panelId !== panelId) return null;
  const params = toParams(search);
  if (!params.has(urlParamKey)) return null;
  params.delete(urlParamKey);
  return params.toString();
}
