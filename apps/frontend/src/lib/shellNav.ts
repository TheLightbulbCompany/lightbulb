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
