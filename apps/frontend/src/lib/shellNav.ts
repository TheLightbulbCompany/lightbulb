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
  kind: "issue" | "routine" | "project" | "goal" | "run",
  id: string,
): string {
  return `/chat?panel=teams-${kind}-detail&detailId=${encodeURIComponent(id)}`;
}

// The bundled paperclip skill ("Company-prefixed URLs (required)") instructs
// agents to write every internal link as a native Paperclip path —
// `/ISOA/issues/ISOA-171`, `/ISOA/approvals/<id>`, `/ISOA/agents/<key>/runs/<id>`.
// Those routes do not exist in this app (the /teams/* tree was retired without a
// redirect), so a bare anchor left the shell and 404'd. Maps such a path onto its
// in-shell equivalent, or null when the href isn't one (external URLs, /api/…,
// already-in-app /chat?… links — the uppercase prefix keeps those out).
//
// Issue identifiers need no lookup: upstream Paperclip resolves `ISOA-171` for
// all /issues/:id routes and the Teams BFF passes issue_id straight through.
const PAPERCLIP_LINK =
  /^\/[A-Z][A-Z0-9]{0,9}\/(issues|approvals|projects|agents)\/([^/?#]+)(?:\/runs\/([^/?#]+))?\/?(?:\?[^#]*)?(#[^?]*)?$/;

export function shellUrlFromPaperclipPath(href?: string | null): string | null {
  const m = href ? PAPERCLIP_LINK.exec(href) : null;
  if (!m) return null;
  const [, section, id, runId, hash = ""] = m;
  if (runId) return teamsDetailUrl("run", runId);
  if (section === "issues") return teamsDetailUrl("issue", id) + hash;
  // No detail panel takes these ids — agent url-keys and project url-keys are
  // not the uuids our panels resolve, and approvals only ever render inside the
  // Inbox. Land on the list panel that owns them; a list beats a dead link.
  return teamsPanelUrl(section === "approvals" ? "inbox" : section);
}

// Commit an in-shell URL with no anchor to fall through to — a button, or a
// notice's action. The App Router integrates with pushState, so this is the
// whole client-side swap; handleShellLinkClick is this plus the
// modified-click bail-out.
export function navigateShell(url: string): void {
  window.history.pushState(null, "", url);
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
  navigateShell(url);
}
