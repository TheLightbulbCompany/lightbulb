// =============================================================================
// Root domains — per-environment parameters, not literals
// =============================================================================
// `rootDomain` is the PRODUCT domain: the frontend and API hosts hang off it.
// `platformMailDomain` is the domain the platform sender lives on. The two are
// the same value today but stay separate parameters, threaded from `lib/app.ts`
// through `LightbulbStageProps`. The constants below are the defaults every
// stack falls back to when a prop is omitted (unit-test synths).
// =============================================================================

/** Product domain — frontend and API hosts. */
export const ROOT_DOMAIN = "thelightbulb.company";

/** Domain the platform sender lives on (the SES identity is on the apex). */
export const PLATFORM_MAIL_DOMAIN = "thelightbulb.company";

/** DEV product domain. Same value as ROOT_DOMAIN. */
export const DEV_ROOT_DOMAIN = "thelightbulb.company";

/** The retired product domain's second life: the launch-competition site (Vercel only). */
export const LAUNCH_DOMAIN = "flickmeon.ai";
