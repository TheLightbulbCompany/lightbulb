import { clerkMiddleware } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

import {
  ROOT_DOMAIN,
  SITE_URL,
  parseAdminHosts,
} from "@/lib/domain";

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
 * Any `/admin*` path is a 404 on every host: off an admin host so probes
 * cannot confirm the path exists (CEO A1), and on one because old links never
 * redirect (owner, 2026-11-25). On an admin host the admin lives at the root:
 * `/<path>` is rewritten to `/admin/<path>` (the address bar keeps the short
 * path). Clerk auth paths, Next internals and API routes are left alone
 * (see isAdminHostPassthroughPath).
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

const _clerkMiddleware = clerkMiddleware(async (_auth, req) => {
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

  // No page is behind sign-in any more except the admin, whose layout gates itself.
});

export default function middleware(req: NextRequest, evt: Parameters<typeof _clerkMiddleware>[1]) {
  const host = (req.headers.get("host") ?? "").toLowerCase();
  const canonicalHost = decideCanonicalHostRedirect(host, req.nextUrl.pathname, req.nextUrl.search);
  if (canonicalHost) return NextResponse.redirect(canonicalHost.to, 308);
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
