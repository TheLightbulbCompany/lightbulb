// =============================================================================
// Root domains — per-environment parameters, not literals
// =============================================================================
// `rootDomain` is the PRODUCT domain: the frontend, API, WebSocket, browser-view,
// Clerk and Paperclip company hosts all hang off it.
//
// `platformMailDomain` is the domain the platform sender and agent mail live on.
// It is NO LONGER the root domain: mail moved to its own domain, so the product
// hostnames and the mail hostnames now change independently.
//
// Both are threaded from `lib/app.ts` through `Isol8StageProps` into every stack
// that builds a hostname, so a per-environment change is a one-line edit at the
// stage construction site. The constants below are the defaults every stack
// falls back to when a prop is omitted (unit-test synths), and the value the
// tests assert against.
// =============================================================================

/** Product domain — frontend, API, WS, browser-view, Clerk, company hosts. */
export const ROOT_DOMAIN = "flickmeon.ai";

/**
 * Domain the platform sender + agent mail live on — deliberately NOT the
 * product domain.
 *
 * The apex carries Google Workspace for humans (`support@`) and the SES
 * platform senders (`hello@`, `tickets@`, `admin@`); every company subdomain
 * (`{agent}@{slug}.thelightbulb.company`) is SES-only, authorized by the one
 * apex domain identity. See ses-inbound-stack.ts.
 */
export const PLATFORM_MAIL_DOMAIN = "thelightbulb.company";

/**
 * The mail domain being retired.
 *
 * Every agent address already minted into DynamoDB is `{agent}@{slug}.isol8.co`
 * and nothing migrates them, so isol8.co keeps its records, its SES identity
 * and its receipt-rule recipients until a backfill re-mints those addresses on
 * PLATFORM_MAIL_DOMAIN. Delete this constant together with the legacy leg in
 * ses-inbound-stack.ts and the legacy identity ARN in service-stack.ts — that
 * is the retirement step, and it is not this one.
 */
export const LEGACY_MAIL_DOMAIN = "isol8.co";

/**
 * Domain the Clerk frontend API hangs off (`clerk.{CLERK_DOMAIN}`).
 *
 * Its own parameter, NOT `= ROOT_DOMAIN`, because Clerk is a third independent
 * track. Clerk's host does not move because we deploy — it moves when someone
 * performs "Change domain" on the Clerk instance, which mints a new publishable
 * key and needs new DNS records and certificates.
 *
 * Derived from ROOT_DOMAIN, flipping the product domain would point the
 * WebSocket Lambda authorizer at `https://clerk.{new}/.well-known/jwks.json`
 * before that host existed. The authorizer could not fetch JWKS, so every
 * `$connect` would be rejected: chat down fleet-wide, from an infrastructure
 * deploy that looks nothing to do with auth.
 *
 * Same value as ROOT_DOMAIN today, so this changes no synthesized output. It
 * exists so the product domain and the auth domain can move independently, and
 * is repointed in its own change once the Clerk domain has actually moved.
 */
export const CLERK_DOMAIN = "isol8.co";

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
// (Now identical to ROOT_DOMAIN — dev led, prod followed. The symbol stays so
// the two can diverge again for a future migration without re-threading props.)

/**
 * The domain being retired. `WildcardCert` in DnsStack stays pinned to this so
 * its cross-stack Export never changes while the network + api stacks import
 * it — see the DnsStack docstring. Remove only once nothing imports that export.
 */
export const LEGACY_ROOT_DOMAIN = "isol8.co";
