/**
 * The one place the product's domain is written down.
 *
 * Everything host-shaped in the web app derives from here: canonical URLs and
 * JSON-LD, the admin host allowlist (`src/middleware.ts`), the
 * CSP own-origins (`next.config.ts`). Moving domains is an edit to this file — or to
 * `NEXT_PUBLIC_APP_URL` — not a grep.
 *
 * `NEXT_PUBLIC_APP_URL` is deliberately OPTIONAL: it is set in no Vercel
 * environment today, so the literal below is what actually ships. Keep it a
 * working default, never a required var.
 *
 * Imported by `next.config.ts` (Node) and `src/middleware.ts` (edge) as well as
 * app code, so it must stay dependency-free, side-effect-free, and unable to
 * throw — a module-scope throw here takes down every request.
 */

/** Absolute origin the public site is served from, no trailing slash. */
export const SITE_URL = (
  process.env.NEXT_PUBLIC_APP_URL ?? "https://thelightbulb.company"
).replace(/\/+$/, "");

/** Bare host of {@link SITE_URL} — the root every sibling host is built from. */
export const ROOT_DOMAIN = SITE_URL.replace(/^https?:\/\//, "").replace(
  /\/.*$/,
  "",
);

/**
 * Custom URL scheme for native deep links
 * (`flickmeon://auth`, `flickmeon://checkout_redirect/...`). Mirrored by the mobile app's own config;
 * that is a separate registry, so a change here is not a change there.
 *
 * NOT the `isol8:*` localStorage prefixes — they are not a domain and are not
 * OS-registered.
 */
export const DEEP_LINK_SCHEME = "flickmeon";

/**
 * Where "contact support" actually lands.
 *
 * The product and the mail share one domain now; the address is still its own
 * constant so it does not silently follow a future product-domain move.
 *
 * One constant because the previous address, `support@isol8.co`, outlived the
 * brand in ten hand-typed places, including the only escape hatch on the
 * onboarding screen. An address that appears once cannot rot in a corner.
 */
export const SUPPORT_EMAIL = "support@thelightbulb.company";

/** `href` form of {@link SUPPORT_EMAIL}, for every "Contact support" link. */
export const SUPPORT_MAILTO = `mailto:${SUPPORT_EMAIL}`;

/** `admin.<root>` etc. — hosts allowed to serve the /admin route group. */
/**
 * The product domains this build answers on.
 *
 * Now a single canonical domain: isol8.co is retired, so admitting it would
 * allow a host that no longer resolves. It is still derived rather than
 * literal, because `SITE_URL` is the one place the domain is written down.
 *
 * The bug this list exists to prevent: `admin.dev.thelightbulb.company/admin` returned
 * 404 while the allowlist held only the old apex, because `SITE_URL` is one
 * literal shared by both deployments.
 */
export const PRODUCT_DOMAINS = Array.from(
  new Set([ROOT_DOMAIN]),
);

export const DEFAULT_ADMIN_HOSTS = [
  ...PRODUCT_DOMAINS.flatMap((d) => [`admin.${d}`, `admin.dev.${d}`]),
  "admin.localhost:3000",
];

// NEXT_PUBLIC_ADMIN_HOSTS is set in no deployed environment, so the derived
// default above is what actually runs everywhere — there is no hot-fix lever, a
// wrong admin host needs a rebuild. (`.env.example` documents the same set;
// keep the two in step.)
export function parseAdminHosts(raw: string | undefined): Set<string> {
  const source = raw && raw.length > 0 ? raw.split(",") : DEFAULT_ADMIN_HOSTS;
  return new Set(
    source
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean),
  );
}

/** Where sign-in and sign-up land: the admin's root on an admin host, Spark's page everywhere else. */
export function authFallbackUrl(host: string | undefined): string {
  return host && parseAdminHosts(process.env.NEXT_PUBLIC_ADMIN_HOSTS).has(host.toLowerCase()) ? "/" : "/os";
}

/**
 * The retired product domain, still attached to this Vercel project. Every
 * request on these hosts, static files included, is 308d to the product
 * site by `redirects()` in `next.config.ts`, which runs before middleware.
 */
export const LAUNCH_HOSTS = ["flickmeon.ai", "www.flickmeon.ai", "dev.flickmeon.ai"];
