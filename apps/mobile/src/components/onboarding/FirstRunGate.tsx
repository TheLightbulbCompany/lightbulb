/**
 * First-run eligibility host — the mobile counterpart of the web's
 * ProvisioningStepper branch (`!containerOnboarded && !firstRun.briefSubmitted
 * → <FirstRunFunnel/>`, see apps/frontend/src/components/chat/ProvisioningStepper.tsx).
 *
 * Mounts INSIDE ProvisionGate (see (app)/_layout.tsx), so blocked /
 * team-pending interstitials always win: ProvisionGate only ever renders
 * `children` — i.e. this component — once it has resolved past those.
 * Renders the funnel full-screen INSTEAD of the tab shell for an eligible
 * user; everyone else falls through to children immediately.
 *
 * Eligibility = ANY owner (org or personal) whose CONTAINER hasn't been
 * onboarded yet and who hasn't locally submitted the brief. "Have we
 * onboarded?" is a PER-CONTAINER fact, not per-user (mobile
 * onboarding-parity Slice 2, matching the web): an org shares one
 * container, so onboarding happens once for the whole org — the backend
 * stamps `container.onboarded_at` and surfaces it as `onboarded` on
 * `/container/status`, so once anyone on the container completes the
 * funnel, no one on it — including a second org member whose own local
 * marker is empty — sees it again. Before this slice, this gate only ever
 * probed `/billing/account` for `trial_local`, which is `false` for every
 * org owner — org owners never saw the funnel at all. That probe is gone.
 *
 * The status comes from `useContainerStatusContext()` — the SAME
 * `/container/status` poll ProvisionGate already runs, read via React
 * Context instead of a second fetch (collapses what used to be two
 * independent pollers into one). `briefSubmitted` stays a local
 * Clerk-metadata latch (reactive: `writeFirstRun({briefSubmitted:true})`
 * re-renders `useUser` and flips the gate off without navigation), so the
 * submitter doesn't see a flash between submitting and the next poll
 * flipping `onboarded`.
 *
 * UNKNOWN is treated as "don't show, don't block": anything other than a
 * definitive `{kind:"ready", onboarded:false}` — loading, starting, parked,
 * unreachable, or no provider above this component at all — falls through
 * to children. There is deliberately no blocking loading frame here (unlike
 * the old billing probe): trapping the user on a spinner over a
 * transient/unresolved read is worse than the funnel appearing one poll
 * late once `ready` + `onboarded:false` actually resolves.
 */
import { useUser } from "@clerk/clerk-expo";
import type { ReactNode } from "react";

import { useContainerStatusContext } from "../../lib/containerStatusContext";
import { readFirstRun } from "../../lib/firstRun";
import { FirstRunFunnel } from "./FirstRunFunnel";

export function FirstRunGate({ children }: { children: ReactNode }) {
  const { user } = useUser();
  const status = useContainerStatusContext();

  // Reactive: writeFirstRun({briefSubmitted:true}) re-renders useUser and
  // flips the gate off without any navigation.
  const briefSubmitted = readFirstRun(user).briefSubmitted === true;

  // `user` guard is load-bearing beyond null-safety: submitting writes
  // user.unsafeMetadata, so without a loaded Clerk user the funnel would be
  // a dead-end form (see FirstRunFunnel). `status?.onboarded === false`
  // (strict) rather than `!== true` so an unresolved/absent field never
  // reads as eligible.
  const eligible =
    !!user &&
    !briefSubmitted &&
    status?.kind === "ready" &&
    status.onboarded === false;

  if (eligible) return <FirstRunFunnel />;
  return <>{children}</>;
}
