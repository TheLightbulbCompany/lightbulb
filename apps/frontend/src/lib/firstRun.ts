/**
 * First-run ("onboarding aha") UI-flow state.
 *
 * Per the 2026-07-24 architecture decision (spec PR-6, BOOTSTRAP), Clerk
 * `unsafeMetadata` is a WRITE-ONLY presentation cache — it may be written
 * for an optimistic pre-first-poll paint, but no backend code and no client
 * gating logic may read it as authority (a forged/stale value must produce
 * at most a one-frame paint flicker, never a wrong gate). `briefSubmitted`
 * is the one surviving field: FirstRunFunnel writes it on submit so the
 * funnel dismisses THIS session without waiting for the next
 * `/container/status` poll to catch up; ProvisioningStepper's same-session
 * "flash bridge" reads it for exactly that. The durable authority for "has
 * this owner submitted the funnel" is the backend's `funnel.state` on the
 * `StatusContract` (`GET /container/status`), not this.
 *
 * `started`/`completed` (the client-side first-run-kickoff latch) were
 * deleted in the same PR: the kickoff itself is server-dispatched now
 * (`core/services/bootstrap_dispatcher.py`), and its completion is the
 * backend's own observed `chat.final` (`bootstrap.state` on the
 * StatusContract), not a client write. The brief CONTENT never lived here —
 * it is BRIEF.md in Friday's workspace (backend POST /onboarding/brief).
 */
import type { useUser } from "@clerk/nextjs";

/**
 * Clerk's UserResource, derived from useUser() — @clerk/types is not a
 * direct dependency of this package, so the type is not importable here.
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
