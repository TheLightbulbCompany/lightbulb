import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

const isProtectedRoute = createRouteMatcher([
  "/chat(.*)",
  "/onboarding",
  "/settings(.*)",
]);

// The standalone `/teams` route tree was absorbed into the unified `/chat`
// shell. These maps translate an old `/teams/*` URL (bookmarks, middle-click
// `<Link>` fallbacks left in panels, command-palette router.push targets) into
// the equivalent in-shell `/chat?panel=teams-*` deep link.
//
// List panels reachable as `/teams/<panel>`. Each maps to
// `/chat?panel=teams-<panel>&view=control`. `members` is intentionally absent:
// the Members tab was retired and its in-shell key redirects to Settings.
const TEAMS_LIST_PANELS = new Set([
  "dashboard",
  "inbox",
  "issues",
  "routines",
  "goals",
  "projects",
  "org-chart",
  "skills",
  "activity",
  "settings",
  "agents",
  "costs",
]);

// Detail routes `/teams/<segment>/<id>` map to a `teams-<panel>-detail` key with
// the id threaded through `?detailId`.
const TEAMS_DETAIL_PANELS: Record<string, string> = {
  issues: "teams-issue-detail",
  goals: "teams-goal-detail",
  projects: "teams-project-detail",
  routines: "teams-routine-detail",
};

/**
 * Pure `/teams/*` path -> in-shell `/chat?...` target. Returns null when the
 * path isn't a retired Teams route. Exported for unit testing without a
 * Next.js request object.
 */
export function decideTeamsRedirect(pathname: string): string | null {
  if (pathname === "/teams" || pathname === "/teams/") {
    return "/chat?panel=teams-dashboard&view=control";
  }
  if (!pathname.startsWith("/teams/")) return null;

  const segments = pathname.slice("/teams/".length).split("/").filter(Boolean);

  // Detail route: /teams/<segment>/<id> (e.g. /teams/issues/iss_42).
  if (segments.length === 2) {
    const detailPanel = TEAMS_DETAIL_PANELS[segments[0]];
    if (detailPanel) {
      const id = encodeURIComponent(segments[1]);
      return `/chat?panel=${detailPanel}&detailId=${id}&view=control`;
    }
  }

  // List route: /teams/<panel>.
  if (segments.length === 1 && TEAMS_LIST_PANELS.has(segments[0])) {
    return `/chat?panel=teams-${segments[0]}&view=control`;
  }

  // Any other /teams/* path (deeper detail routes like agent runs/approvals,
  // unknown panels) falls back to the dashboard rather than 404ing.
  return "/chat?panel=teams-dashboard&view=control";
}

const DEFAULT_ADMIN_HOSTS = "admin.isol8.co,admin.dev.isol8.co,admin.localhost:3000";

function parseAdminHosts(raw: string | undefined): Set<string> {
  const source = raw && raw.length > 0 ? raw : DEFAULT_ADMIN_HOSTS;
  return new Set(
    source
      .split(",")
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
  );
}

const ADMIN_HOSTS = parseAdminHosts(process.env.NEXT_PUBLIC_ADMIN_HOSTS);

export type AdminHostDecision =
  | { kind: "passthrough" }
  | { kind: "not_found" }
  | { kind: "redirect"; to: string };

// Paths that must passthrough on admin hosts even though they sit outside
// /admin*. AdminLayout (server component) redirects unauth users to
// /sign-in; without this allowlist the gate immediately bounces /sign-in
// back to /admin, AdminLayout redirects to /sign-in again — infinite 307
// loop (ERR_TOO_MANY_REDIRECTS). Clerk's catchall routes (e.g.
// /sign-in/factor-one, /sign-in/sso-callback) are also included.
function isAdminHostAllowedNonAdminPath(pathname: string): boolean {
  return (
    pathname === "/sign-in" ||
    pathname.startsWith("/sign-in/") ||
    pathname === "/sign-up" ||
    pathname.startsWith("/sign-up/")
  );
}

/**
 * Pure host/path -> decision function for the admin gate. Exported so the
 * branching logic can be unit-tested without a Next.js request object.
 *
 * Defense in depth (CEO A1): unknown hosts hitting `/admin*` get a 404, not a
 * 403 — we don't want host-header probes to confirm the path even exists.
 * Conversely, an admin host that strays off `/admin` is funneled back to
 * `/admin` so admins can't accidentally land on the public app. Clerk's
 * auth paths (`/sign-in`, `/sign-up`, and their catchall sub-paths) are
 * exempted from that funnel so AdminLayout's sign-in redirect can resolve
 * instead of looping back through the gate.
 */
export function decideAdminHostRouting(
  host: string | null | undefined,
  pathname: string,
  adminHosts: Set<string> = ADMIN_HOSTS,
): AdminHostDecision {
  const normalizedHost = (host ?? "").toLowerCase();
  const isAdminHost = adminHosts.has(normalizedHost);
  const isAdminPath = pathname === "/admin" || pathname.startsWith("/admin/");

  if (isAdminPath && !isAdminHost) {
    return { kind: "not_found" };
  }
  if (isAdminHost && !isAdminPath) {
    if (isAdminHostAllowedNonAdminPath(pathname)) {
      return { kind: "passthrough" };
    }
    return { kind: "redirect", to: "/admin" };
  }
  return { kind: "passthrough" };
}

// Hostnames whose traffic is handled by the paperclip-proxy upstream
// via a Next-level rewrite (next.config.ts `beforeFiles`). Middleware
// must passthrough for these or it'll run Clerk auth on a non-Isol8
// host (where there's no Clerk session) and redirect users away
// before the rewrite gets a chance to proxy the request.
//
// IMPORTANT: this check has to run BEFORE clerkMiddleware wraps the
// request — otherwise Clerk's own dev-browser handshake fires (returns
// a 307 to clerk.accounts.dev/v1/client/handshake?...) regardless of
// what our inner callback does. Wrapping clerkMiddleware from outside
// is the only way to fully bypass it for these hosts.
const PAPERCLIP_HOSTS = new Set([
  "company.isol8.co",
  "dev.company.isol8.co",
]);

const _clerkMiddleware = clerkMiddleware(async (auth, req) => {
  const host = (req.headers.get("host") ?? "").toLowerCase();
  const decision = decideAdminHostRouting(host, req.nextUrl.pathname);

  if (decision.kind === "not_found") {
    return new NextResponse(null, { status: 404 });
  }
  if (decision.kind === "redirect") {
    const url = req.nextUrl.clone();
    url.pathname = decision.to;
    return NextResponse.redirect(url);
  }

  // Absorb the retired `/teams/*` routes into the unified `/chat` shell. Done
  // server-side (NextResponse.redirect, no client history entry) so old
  // bookmarks + `<Link href="/teams/...">` fallbacks land on the equivalent
  // in-shell deep link.
  const teamsTarget = decideTeamsRedirect(req.nextUrl.pathname);
  if (teamsTarget) {
    return NextResponse.redirect(new URL(teamsTarget, req.nextUrl.origin));
  }

  const authObj = await auth();

  if (isProtectedRoute(req)) {
    if (!authObj.userId) {
      return authObj.redirectToSignIn();
    }
  }
});

export default function middleware(req: NextRequest, evt: Parameters<typeof _clerkMiddleware>[1]) {
  const host = (req.headers.get("host") ?? "").toLowerCase();
  if (PAPERCLIP_HOSTS.has(host)) {
    // Bypass clerkMiddleware entirely so Clerk's dev-browser handshake
    // doesn't 307-redirect the request away from the beforeFiles rewrite.
    return NextResponse.next();
  }
  return _clerkMiddleware(req, evt);
}

export const config = {
  matcher: [
    // Skip Next.js internals and all static files, unless found in search params
    '/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)',
    // Always run for API routes
    '/(api|trpc)(.*)',
  ],
};
