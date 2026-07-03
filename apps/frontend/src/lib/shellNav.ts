// In-shell navigation state — ?panel, ?agent, ?detailId, post-checkout param
// strips — lives entirely in the query string of the route the shell is
// already on. Committing it through the native History API keeps those
// transitions client-side: the App Router integrates with pushState /
// replaceState (useSearchParams / usePathname update in place), so a panel
// switch costs no RSC round-trip, no middleware pass, and no Suspense
// fallback flash. router.push / router.replace on the force-dynamic /chat
// route pays all three for what is purely presentation state.
//
// push grows a history entry (Back steps panel → panel, detail → list);
// replace canonicalizes in place (param strips, filter dismissals).

function currentUrlWith(params: URLSearchParams): string {
  const qs = params.toString();
  return qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
}

export function pushSearchParams(params: URLSearchParams): void {
  window.history.pushState(null, "", currentUrlWith(params));
}

export function replaceSearchParams(params: URLSearchParams): void {
  window.history.replaceState(null, "", currentUrlWith(params));
}

// Canonical in-shell URLs for Teams surfaces. These are the ONLY URL forms
// for Teams content — the retired /teams/* route tree is not supported and
// has no redirect. Anchors keep one of these as their href so native
// new-tab / middle-click still works; plain left-clicks should go through
// handleShellLinkClick so the swap stays client-side.
export function teamsPanelUrl(panel: string): string {
  return `/chat?panel=teams-${panel}`;
}

export function teamsDetailUrl(
  kind: "issue" | "routine" | "project" | "goal",
  id: string,
): string {
  return `/chat?panel=teams-${kind}-detail&detailId=${encodeURIComponent(id)}`;
}

// Anchor onClick companion for the URL builders above: plain left-clicks
// commit through pushState (client-side swap); modified clicks and non-left
// buttons fall through to the href's native behavior.
export function handleShellLinkClick(
  e: {
    defaultPrevented: boolean;
    metaKey: boolean;
    ctrlKey: boolean;
    shiftKey: boolean;
    altKey: boolean;
    button: number;
    preventDefault(): void;
  },
  url: string,
): void {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)
    return;
  e.preventDefault();
  window.history.pushState(null, "", url);
}
