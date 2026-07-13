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
 * two in sync (both name BRIEF.md, the Set-up issue, 3 starter tasks,
 * the research doc, MISSION.md).
 */
import type { useUser } from "@clerk/nextjs";

/**
 * Clerk's UserResource, derived from useUser() — @clerk/types is not a
 * direct dependency of this package, so the type is not importable here.
 */
type UserResource = NonNullable<ReturnType<typeof useUser>["user"]>;

export interface FirstRunMeta {
  briefSubmitted?: boolean;
  skipped?: boolean;
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
  "Friday — I just set up my company. Read BRIEF.md in your workspace and run my onboarding:",
  "claim the \"Set up\" task on the board, create 3 starter tasks from a quick first pass,",
  "then research my market properly, enrich the tasks with what you find, write the research",
  "doc and MISSION.md, and close out with a summary. Narrate as you go — I'm watching the board.",
].join(" ");
