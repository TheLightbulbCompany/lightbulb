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

import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";

function currentUrlWith(params: URLSearchParams): string {
  const qs = params.toString();
  return qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
}

// A bare /chat entry carries no command, so Back onto it could not restore what
// it showed. The desktop shell registers a namer (ChatHistorySync) that gives
// the entry its view before any push leaves it.
let nameCurrentEntry: (() => string | null) | null = null;
export function setCurrentEntryNamer(namer: (() => string | null) | null): void {
  nameCurrentEntry = namer;
}

function pushUrl(url: string): void {
  const p = new URLSearchParams(window.location.search);
  const name = !p.has("panel") && !p.has("agent") && !p.has("room") ? nameCurrentEntry?.() : null;
  if (name) replaceSearchParams(new URLSearchParams(name));
  window.history.pushState(null, "", url);
}

export function pushSearchParams(params: URLSearchParams): void {
  pushUrl(currentUrlWith(params));
}

export function replaceSearchParams(params: URLSearchParams): void {
  window.history.replaceState(null, "", currentUrlWith(params));
}

// The bundled paperclip skill ("Company-prefixed URLs (required)") instructs
// agents to write every internal link as a native Paperclip path —
// `/ISOA/issues/ISOA-171`, `/ISOA/approvals/<id>`, `/ISOA/agents/<key>/runs/<id>`,
// `/ISOA/goals/<id>`. Those routes do not exist in this app (the /teams/* tree
// was retired without a redirect), so a bare anchor left the shell and 404'd.
// Maps such a path onto its in-shell equivalent, or null when the href isn't
// one (external URLs, /api/…, already-in-app /chat?… links — the uppercase
// prefix keeps those out).
//
// The company prefix is REQUIRED by the skill (comment-style.md), but agents
// sometimes drop it anyway when they hand-write an issue/approval link inline
// — those two sections tolerate a missing prefix as a defensive fallback.
// Goals/agents/projects keep the prefix mandatory: a bare `/agents/<key>` or
// `/projects/<key>` is exactly as likely to be someone else's route as an
// agent-dropped prefix, and there is no company-scoped list to fall back to
// safely the way an unprefixed issue/approval id still uniquely resolves.
//
const PAPERCLIP_LINK =
  /^\/(?:([A-Z][A-Z0-9]{0,9})\/)?(issues|approvals|projects|agents|goals)\/([^/?#]+)(?:\/runs\/([^/?#]+))?\/?(?:\?[^#]*)?(#[^?]*)?$/;

// The retired /teams/* tree: agents still write task links as /teams/tasks/<id>
// (and /teams/issues/<id>), which 404 as they stand. Read them as unprefixed issue links.
const RETIRED_TASK_LINK = /^\/teams\/(?:tasks|issues)\//;

export function shellUrlFromPaperclipPath(href?: string | null): string | null {
  const m = href ? PAPERCLIP_LINK.exec(href.replace(RETIRED_TASK_LINK, "/issues/")) : null;
  if (!m) return null;
  const [, prefix, section, id, runId] = m;
  if (!prefix && section !== "issues" && section !== "approvals") return null;
  if (section === "agents" && !runId) return agentInspectorUrl(id);
  // The Paperclip board is gone (Phase 1 task 7): an old issue, approval, run, goal or project link
  // opens Activity, where tasks live (the registry aliases every retired teams-* key there).
  return "/chat?panel=activity";
}

// Commit an in-shell URL with no anchor to fall through to — a button, or a
// notice's action. The App Router integrates with pushState, so this is the
// whole client-side swap; handleShellLinkClick is this plus the
// modified-click bail-out.
export function navigateShell(url: string): void {
  pushUrl(url);
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
