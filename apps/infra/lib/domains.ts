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
// Both are threaded from `lib/app.ts` through `LightbulbStageProps` into every stack
// that builds a hostname, so a per-environment change is a one-line edit at the
// stage construction site. The constants below are the defaults every stack
// falls back to when a prop is omitted (unit-test synths), and the value the
// tests assert against.
// =============================================================================

/** Product domain — frontend, API, WS, browser-view, Clerk, company hosts. */
export const ROOT_DOMAIN = "thelightbulb.company";

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
 * The mail domain being retired. EMPTY = none; the retirement is done.
 *
 * This held `isol8.co` while agent addresses were still minted there. Its own
 * precondition -- "until a backfill re-mints those addresses on
 * PLATFORM_MAIL_DOMAIN" -- was met on 2026-08-27:
 * `scripts/rekey_agent_email_domain.py` ran against BOTH envs (18 prod + 6 dev
 * moved, 0 collisions), the superseded alias rows were deleted, and all three
 * SES tenants were re-associated onto `identity/thelightbulb.company` and
 * dissociated from `identity/isol8.co`. Zero addresses remain on the old domain.
 *
 * Kept as a parameter rather than deleted: it is the switch for retiring ANY
 * mail domain, and `publishLegacy` in ses-inbound-stack.ts already reads empty
 * as "publish nothing". Set it to a domain to run this play again.
 */
export const LEGACY_MAIL_DOMAIN = "";

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
export const CLERK_DOMAIN = "thelightbulb.company";

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
export const DEV_ROOT_DOMAIN = "thelightbulb.company";
// (Dev leads the 2026-10 move to thelightbulb.company; prod follows.)

/**
 * The product domain being moved away from. Pins its certificate Export in
 * DnsStack while the consumer stacks repoint. Clear it only after
 * `cloudformation list-imports` shows no importer in either env.
 *
 * NOT a statement about that domain's hosted zone, which stays.
 */
export const LEGACY_ROOT_DOMAIN = "flickmeon.ai";

/** The retired product domain's second life: the launch-competition site (Vercel only). */
export const LAUNCH_DOMAIN = "flickmeon.ai";
