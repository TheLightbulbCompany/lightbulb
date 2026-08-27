/**
 * The one place the product's domain is written down.
 *
 * Everything host-shaped in the web app derives from here: canonical URLs and
 * JSON-LD, the admin + paperclip host allowlists (`src/middleware.ts`), the
 * company-host rewrites and CSP own-origins (`next.config.ts`), and the
 * desktop-updater host echo. Moving domains is an edit to this file — or to
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
  process.env.NEXT_PUBLIC_APP_URL ?? "https://flickmeon.ai"
).replace(/\/+$/, "");

/** Bare host of {@link SITE_URL} — the root every sibling host is built from. */
export const ROOT_DOMAIN = SITE_URL.replace(/^https?:\/\//, "").replace(
  /\/.*$/,
  "",
);

/**
 * Custom URL scheme the desktop app registers for deep links
 * (`flickmeon://auth`, `flickmeon://checkout_redirect/...`). Mirrored by the mobile app's own config and
 * by `apps/desktop/src-tauri/tauri.conf.json`; those are separate registries,
 * so a change here is not a change there.
 *
 * NOT the internal `isol8-file://` asset scheme, and NOT the `isol8:*`
 * localStorage prefixes — neither is a domain, and neither is OS-registered.
 */
export const DEEP_LINK_SCHEME = "flickmeon";

/** `admin.<root>` etc. — hosts allowed to serve the /admin route group. */
/**
 * The product domains this build answers on.
 *
 * Now a single canonical domain: isol8.co is retired, so admitting it would
 * allow a host that no longer resolves. It is still derived rather than
 * literal, because `SITE_URL` is the one place the domain is written down.
 *
 * The bug this list exists to prevent: `admin.dev.flickmeon.ai/admin` returned
 * 404 while the allowlist held only the old apex, because `SITE_URL` is one
 * literal shared by both deployments.
 */
const PRODUCT_DOMAINS = Array.from(
  new Set([ROOT_DOMAIN]),
);

export const DEFAULT_ADMIN_HOSTS = [
  ...PRODUCT_DOMAINS.flatMap((d) => [`admin.${d}`, `admin.dev.${d}`]),
  "admin.localhost:3000",
];

/** Hosts proxied wholesale to the per-tenant Paperclip BFF. */
export const PAPERCLIP_HOSTS = PRODUCT_DOMAINS.flatMap((d) => [
  `company.${d}`,
  `dev.company.${d}`,
]);
