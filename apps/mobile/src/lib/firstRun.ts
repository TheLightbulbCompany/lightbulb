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
 * Per the 2026-07-24 architecture decision (spec PR-6, BOOTSTRAP), Clerk
 * `unsafeMetadata` is a WRITE-ONLY presentation cache — it may be written
 * for an optimistic pre-first-poll paint, but no backend code and no client
 * gating logic may read it as authority. `briefSubmitted` is the one
 * surviving field, written on submit so the funnel dismisses THIS session
 * without waiting for the next poll. `started`/`completed` (the client-side
 * first-run-kickoff latch) were deleted in the same PR: the kickoff itself
 * is server-dispatched now (`core/services/bootstrap_dispatcher.py`), and
 * its completion is the backend's own observed `chat.final`, not a client
 * write.
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
