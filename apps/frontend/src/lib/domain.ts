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
  process.env.NEXT_PUBLIC_APP_URL ?? "https://isol8.co"
).replace(/\/+$/, "");

/** Bare host of {@link SITE_URL} — the root every sibling host is built from. */
export const ROOT_DOMAIN = SITE_URL.replace(/^https?:\/\//, "").replace(
  /\/.*$/,
  "",
);

/**
 * Custom URL scheme the desktop app registers for deep links (`lightbulbai://auth`,
 * `lightbulbai://checkout_redirect/...`). Mirrored by the mobile app's own config and
 * by `apps/desktop/src-tauri/tauri.conf.json`; those are separate registries,
 * so a change here is not a change there.
 *
 * NOT the internal `lightbulb-file://` asset scheme, and NOT the `lightbulb:*`
 * localStorage prefixes — neither is a domain.
 */
export const DEEP_LINK_SCHEME = "lightbulbai";

/** `admin.<root>` etc. — hosts allowed to serve the /admin route group. */
export const DEFAULT_ADMIN_HOSTS = [
  `admin.${ROOT_DOMAIN}`,
  `admin.dev.${ROOT_DOMAIN}`,
  "admin.localhost:3000",
];

/** Hosts proxied wholesale to the per-tenant Paperclip BFF. */
export const PAPERCLIP_HOSTS = [
  `company.${ROOT_DOMAIN}`,
  `dev.company.${ROOT_DOMAIN}`,
];
