// The URL is a ONE-WAY retained command channel (spec §6): inbound ?panel=…
// is a navigation intent the shell executes as open-or-focus; outbound, only
// explicit navigations write it. Focus and layout changes never touch it.
import {
  describePanel,
  durableParamsFor,
  ONE_SHOT_PARAMS,
  NON_COMMAND_PARAMS,
  type PanelDescriptor,
} from "./panelRegistry";

export interface WorkspaceCommand {
  descriptor: PanelDescriptor;
  oneShots: Record<string, string>;
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

  const panelKey = all.panel;
  delete all.panel;

  if (!panelKey) {
    // Bare ?agent= (marketplace deploy-success) = open that agent's chat.
    if (all.agent) {
      const descriptor = describePanel("chat", { agent: all.agent });
      return descriptor ? { descriptor, oneShots } : null;
    }
    return null;
  }

  const descriptor = describePanel(panelKey, all);
  return descriptor ? { descriptor, oneShots } : null;
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
  if (!command || command.descriptor.panelId !== closedPanelId) return null;
  const params = toParams(search);
  const before = params.toString();
  for (const k of durableParamsFor(closedPanelId)) params.delete(k);
  for (const k of ONE_SHOT_PARAMS) params.delete(k);
  const result = params.toString();
  return result === before ? null : result;
}

// All chat URL forms now name Messages directly.
export { clearPanelFromUrl as clearClosedPanelFromUrl };

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
