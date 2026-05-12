import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

const isProtectedRoute = createRouteMatcher([
  "/chat(.*)",
  "/onboarding",
  "/settings(.*)",
  "/teams(.*)",
]);

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
