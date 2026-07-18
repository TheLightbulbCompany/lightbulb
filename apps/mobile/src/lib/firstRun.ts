/**
 * First-run ("onboarding aha") UI-flow state — ported from the web
 * (apps/frontend/src/lib/firstRun.ts); keep the two in sync.
 *
 * Flags live in Clerk user.unsafeMetadata.firstRun and are SHARED with the
 * web app: a user who submitted the brief on either surface never re-sees
 * the funnel on the other. Every writer spreads unsafeMetadata, so sibling
 * keys (`onboarded`, `getStarted`) survive. The brief CONTENT never lives
 * here — it is BRIEF.md in Friday's workspace (backend POST
 * /onboarding/brief).
 *
 * FIRST_RUN_PROMPT is the visible kickoff message auto-sent to Friday. Its
 * runbook counterpart is the BOOTSTRAP.md first-run variant in
 * apps/backend/core/services/onboarding_brief.py — keep the two in sync.
 */
import type { useUser } from "@clerk/clerk-expo";

/**
 * Clerk's UserResource, derived from useUser() — @clerk/types is not a
 * direct dependency, so the type is not importable here (same derived-type
 * pattern as the web lib).
 */
type UserResource = NonNullable<ReturnType<typeof useUser>["user"]>;

export interface FirstRunMeta {
  briefSubmitted?: boolean;
  started?: boolean;
  completed?: boolean;
  path?: "new" | "grow";
}

export function readFirstRun(user: UserResource | null | undefined): FirstRunMeta {
  const meta = user?.unsafeMetadata?.firstRun;
  return (meta && typeof meta === "object" ? meta : {}) as FirstRunMeta;
}

export async function writeFirstRun(
  user: UserResource,
  patch: Partial<FirstRunMeta>,
): Promise<void> {
  await user.update({
    unsafeMetadata: {
      ...user.unsafeMetadata,
      firstRun: { ...readFirstRun(user), ...patch },
    },
  });
}

export const FIRST_RUN_PROMPT = [
  "Friday — I just set up my company. Read BRIEF.md, then really dig into my business and",
  "market and write up what you learn (a market-research doc and a mission doc). Once you",
  "get it, put my three best next moves on the board as tasks. Talk me through it as you go —",
  "I'm watching.",
].join(" ");
