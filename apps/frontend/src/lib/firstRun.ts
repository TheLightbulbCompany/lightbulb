/**
 * First-run ("onboarding aha") UI-flow state.
 *
 * Flags live in Clerk user.unsafeMetadata.firstRun as a SIBLING of the
 * existing `onboarded` and `getStarted` keys — every writer here (and
 * useGetStarted.ts:143) spreads unsafeMetadata, so siblings survive.
 * The brief CONTENT never lives here — it is BRIEF.md in Friday's
 * workspace (backend POST /onboarding/brief).
 *
 * FIRST_RUN_PROMPT is the visible kickoff message auto-sent to Friday
 * (spec §4.C). Its runbook counterpart is the BOOTSTRAP.md first-run
 * variant in apps/backend/core/services/onboarding_brief.py — keep the
 * two in sync: research the business, write the market-research + mission
 * docs, THEN propose three next-move tasks as the closing output (not
 * up-front). The tasks are path-aware — website-first for a NEW business,
 * growth moves for a GROW one — but that branching lives in the BOOTSTRAP,
 * so this visible prompt stays generic for both paths.
 */
import type { useUser } from "@clerk/nextjs";

/**
 * Clerk's UserResource, derived from useUser() — @clerk/types is not a
 * direct dependency of this package, so the type is not importable here.
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
