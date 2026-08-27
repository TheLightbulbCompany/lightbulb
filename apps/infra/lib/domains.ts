// =============================================================================
// Root domains — per-environment parameters, not literals
// =============================================================================
// `rootDomain` is the PRODUCT domain: the frontend, API, WebSocket, browser-view,
// Clerk and Paperclip company hosts all hang off it.
//
// `platformMailDomain` is the domain the platform sender and agent mail live on.
// Today it is the SAME value as the root domain, which is why every mail host
// still resolves under it — it exists as its own parameter so mail can move to a
// dedicated domain without touching any stack.
//
// Both are threaded from `lib/app.ts` through `Isol8StageProps` into every stack
// that builds a hostname, so a per-environment change is a one-line edit at the
// stage construction site. The constants below are the defaults every stack
// falls back to when a prop is omitted (unit-test synths), and the value the
// tests assert against.
// =============================================================================

/** Product domain — frontend, API, WS, browser-view, Clerk, company hosts. */
export const ROOT_DOMAIN = "isol8.co";

/** Domain the platform sender + agent mail live on. Same as ROOT_DOMAIN today. */
export const PLATFORM_MAIL_DOMAIN = ROOT_DOMAIN;

/**
 * DEV product domain — flipped to the new brand ahead of prod so the whole
 * surface (API, WS, browser-view, Clerk CORS, Paperclip origin, Vercel aliases)
 * can be exercised end-to-end before prod is promoted.
 *
 * Mail is deliberately NOT flipped with it: agent mail and the platform sender
 * move to `thelightbulb.company` on their own schedule, and the SES receipt rule
 * set is an account-wide singleton, so dev mail stays on `dev.isol8.co` until
 * that move. The domain flip and the mail move are independent tracks.
 */
export const DEV_ROOT_DOMAIN = "flickmeon.ai";

/**
 * The domain being retired. `WildcardCert` in DnsStack stays pinned to this so
 * its cross-stack Export never changes while the network + api stacks import
 * it — see the DnsStack docstring. Remove only once nothing imports that export.
 */
export const LEGACY_ROOT_DOMAIN = "isol8.co";
