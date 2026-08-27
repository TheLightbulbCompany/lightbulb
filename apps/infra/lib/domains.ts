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
