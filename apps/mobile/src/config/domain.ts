/**
 * The one place the product's web origin and URL scheme are written down.
 *
 * Everything host-shaped in the mobile app derives from here: the outbound
 * "manage this on the web" links (`lib/webLinks.ts`, the control screens, the
 * billing CTA) and the deep-link parsers (`lib/deepLink.ts`).
 *
 * These are DELIBERATELY not EXPO_PUBLIC_* vars: the web links always point at
 * PROD web regardless of which API a build talks to (billing, checkout and
 * OAuth never happen in-app), and the scheme is fixed by the native binary.
 *
 * `APP_SCHEME` mirrors `app.json`'s `scheme` and the desktop app's
 * `flickmeon://` registration — three separate registries, so changing one is not
 * changing the others. `WEB_ORIGIN`'s host must stay in step with `app.json`'s
 * `associatedDomains` (`applinks:…`) or Universal Links stop resolving.
 */

/** Bare root domain — every sibling host (api, api-dev, …) is built from it. */
export const ROOT_DOMAIN = "thelightbulb.company";

/** Absolute origin of the web app, no trailing slash. */
export const WEB_ORIGIN = `https://${ROOT_DOMAIN}`;

/** Custom URL scheme this app registers for deep links. */
export const APP_SCHEME = "flickmeon";
