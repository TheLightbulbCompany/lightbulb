import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

import {
  DEFAULT_ADMIN_HOSTS,
  ROOT_DOMAIN,
  SITE_URL,
} from "@/lib/domain";
import { paperclipLinkTarget } from "@/lib/paperclipLinks";

// `/voice` and its pages only: `/voice(.*)` also matched the public stock samples
// under `/voice-samples/`, which the iPhone app plays without a web session.
export const PROTECTED_ROUTES = [
  "/chat(.*)",
  "/voice",
  "/voice/(.*)",
  "/onboarding",
  "/settings(.*)",
  "/welcome(.*)",
];
const isProtectedRoute = createRouteMatcher(PROTECTED_ROUTES);

// NEXT_PUBLIC_ADMIN_HOSTS is set in no deployed environment, so the derived
// default below is what actually runs everywhere — there is no hot-fix lever, a
// wrong admin host needs a rebuild. (`.env.example` documents the same set;
// keep the two in step.)
function parseAdminHosts(raw: string | undefined): Set<string> {
  const source = raw && raw.length > 0 ? raw.split(",") : DEFAULT_ADMIN_HOSTS;
  return new Set(
    source
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
  );
}

const ADMIN_HOSTS = parseAdminHosts(process.env.NEXT_PUBLIC_ADMIN_HOSTS);

export type AdminHostDecision =
  | { kind: "passthrough" }
  | { kind: "not_found" }
  | { kind: "rewrite"; to: string };

// Paths that must passthrough on admin hosts instead of being rewritten under
// /admin: Clerk's sign-in/sign-up pages (AdminLayout redirects unauth users to
// /sign-in; rewriting it to /admin/sign-in would loop through the layout gate
// forever) and their catchall routes (/sign-in/factor-one, /sign-in/sso-callback),
// plus Next internals, API routes and the PostHog proxy (/ingest), which are not admin pages.
function isAdminHostPassthroughPath(pathname: string): boolean {
  return /^\/(sign-in|sign-up|_next|api|trpc|ingest)(\/|$)/.test(pathname);
}

/**
 * Pure host/path -> decision function for the admin gate. Exported so the
 * branching logic can be unit-tested without a Next.js request object.
 *
 * Defense in depth (CEO A1): unknown hosts hitting `/admin*` get a 404, not a
 * 403 — we don't want host-header probes to confirm the path even exists.
 * On an admin host the admin lives at the root: `/<path>` is rewritten to
 * `/admin/<path>` (the address bar keeps the short path). The old
 * `/admin/<path>` links are a 404 there too: old links never redirect
 * (owner, 2026-11-25). Clerk auth paths, Next internals and API routes are
 * left alone (see isAdminHostPassthroughPath).
 */
export function decideAdminHostRouting(
  host: string | null | undefined,
  pathname: string,
  adminHosts: Set<string> = ADMIN_HOSTS,
): AdminHostDecision {
  const normalizedHost = (host ?? "").toLowerCase();
  const isAdminHost = adminHosts.has(normalizedHost);
  const isAdminPath = pathname === "/admin" || pathname.startsWith("/admin/");

  if (isAdminPath) {
    return { kind: "not_found" };
  }
  if (isAdminHost && !isAdminHostPassthroughPath(pathname)) {
    return { kind: "rewrite", to: pathname === "/" ? "/admin" : `/admin${pathname}` };
  }
  return { kind: "passthrough" };
}

/**
 * Pure pathname -> decision function for the agent-Paperclip-link redirect,
 * exported so it can be unit-tested without a Next.js request object (mirrors
 * decideAdminHostRouting above). The bundled paperclip skill writes internal
 * links as native Paperclip paths (e.g. `/ISOA/issues/ISOA-317`), which have
 * no route in this app — the retired `/teams/*` tree never redirected them,
 * so a direct hit (typed, pasted, or opened from a notification) 404'd.
 * Bounces it onto the Lightbulb place instead.
 */
export function decidePaperclipLinkRedirect(
  pathname: string,
): { kind: "redirect"; to: string } | null {
  const to = paperclipLinkTarget(pathname);
  return to ? { kind: "redirect", to } : null;
}


export type CanonicalHostDecision = { kind: "redirect"; to: string } | null;

/**
 * Consolidate the duplicate www origin onto the apex. A rel=canonical is only
 * a hint; the permanent redirect makes every crawl and backlink resolve to the
 * same host while preserving the requested resource and attribution query.
 */
export function decideCanonicalHostRedirect(
  host: string | null | undefined,
  pathname: string,
  search: string,
): CanonicalHostDecision {
  if ((host ?? "").toLowerCase() !== `www.${ROOT_DOMAIN}`) return null;
  return { kind: "redirect", to: `${SITE_URL}${pathname}${search}` };
}

const _clerkMiddleware = clerkMiddleware(async (auth, req) => {
  const host = (req.headers.get("host") ?? "").toLowerCase();
  const decision = decideAdminHostRouting(host, req.nextUrl.pathname);

  if (decision.kind === "not_found") {
    return new NextResponse(null, { status: 404 });
  }
  if (decision.kind === "rewrite") {
    const url = req.nextUrl.clone();
    url.pathname = decision.to;
    return NextResponse.rewrite(url);
  }

  const paperclipRedirect = decidePaperclipLinkRedirect(req.nextUrl.pathname);
  if (paperclipRedirect) {
    return NextResponse.redirect(new URL(paperclipRedirect.to, req.url), 307);
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
  const canonicalHost = decideCanonicalHostRedirect(host, req.nextUrl.pathname, req.nextUrl.search);
  if (canonicalHost) return NextResponse.redirect(canonicalHost.to, 308);
  // Recall's bot loads /meet-bot in a bare browser: public, and no Clerk handshake.
  if (req.nextUrl.pathname === "/meet-bot" || req.nextUrl.pathname.startsWith("/meet-bot/")) return NextResponse.next();
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
