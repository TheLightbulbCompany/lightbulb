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
 * The decision reads a LATCHED `onboarded`, never the live one. The status
 * here is a poll result that changes every few seconds, and this is the
 * committed onboarding front door (no skip) hosting a form the user is
 * typing into — so re-deciding on each poll meant one idle reap, one wake,
 * or ONE dropped request unmounted the funnel mid-sentence, threw away the
 * in-progress brief, AND dropped the user into the tab shell without having
 * onboarded. The latch arms at the FIRST `ready` carrying a boolean
 * `onboarded` and is never disarmed by a kind, so every transient after it —
 * parked, starting, unreachable, loading, or a context that briefly reads
 * null — holds the last definitive answer instead of overwriting it. Web is
 * immune to exactly this by different means (it gates on
 * `gatePhase === "normal"`, which spans ready/starting/parked, and SWR keeps
 * the last good data across a failed revalidate).
 *
 * UNKNOWN is treated as "don't show, don't block": until a poll has reported
 * a READY container carrying a definitive `onboarded`, this falls through to
 * children. There is deliberately no blocking loading frame here (unlike the
 * old billing probe): trapping the user on a spinner over an unresolved read
 * is worse than the funnel appearing one poll late.
 *
 * The latch arms at the first `ready` SPECIFICALLY, even though a `starting`
 * body carries `onboarded` too (the backend stamps it on every 200) and even
 * though web opens its funnel during a cold start. Web can: its funnel check
 * runs BEFORE its stepper renders, so a stepper can never preempt it. Mobile
 * is wired the other way round — ProvisioningStepper WRAPS `children`, i.e.
 * this gate, so a promoted stepper unmounts whatever this renders. Opening
 * the funnel on a pre-ready `starting` body would therefore re-create a small
 * copy of the very bug above: the user starts typing, PROMOTE_MS elapses, the
 * stepper takes the screen and the in-progress brief is gone. Waiting for
 * `ready` costs a brief pre-ready glimpse of the tab shell — a window in
 * which the container isn't up and chat doesn't work anyway — and the door
 * closes the moment `ready` lands. A glimpse is not a bypass; a discarded
 * brief is a real loss.
 *
 * Blocked / needs-signup / team-pending can't reach this decision at all —
 * ProvisionGate replaces the whole tree for those and `children` (this
 * component) never mounts. Nothing here needs to re-check them, and the
 * latch can't smuggle eligibility into them either: it only ever arms from a
 * `ready` 200 body.
 */
import { useUser } from "@clerk/clerk-expo";
import { useRef, type ReactNode } from "react";

import { useContainerStatusContext } from "../../lib/containerStatusContext";
import { readFirstRun } from "../../lib/firstRun";
import { FirstRunFunnel } from "./FirstRunFunnel";

export function FirstRunGate({ children }: { children: ReactNode }) {
  const { user } = useUser();
  const status = useContainerStatusContext();

  // Reactive: writeFirstRun({briefSubmitted:true}) re-renders useUser and
  // flips the gate off without any navigation.
  const briefSubmitted = readFirstRun(user).briefSubmitted === true;

  // `ready` only — NOT `starting`, which carries `onboarded` as well. See the
  // docstring: the stepper wraps this gate on mobile, so a funnel opened
  // before the container is ready can be unmounted by a promotion mid-brief.
  const readyOnboarded =
    status?.kind === "ready" ? status.onboarded : undefined;

  // Latch: only a definitive boolean arms it, and nothing ever disarms it, so
  // a later transient kind or a dropped request leaves the last real answer
  // standing. Written during render because it is derived purely from the
  // current status — an effect would land a commit late and hand this render
  // the stale value.
  const onboardedRef = useRef<boolean | undefined>(undefined);
  if (typeof readyOnboarded === "boolean") {
    onboardedRef.current = readyOnboarded;
  }

  // `user` guard is load-bearing beyond null-safety: submitting writes
  // user.unsafeMetadata, so without a loaded Clerk user the funnel would be
  // a dead-end form (see FirstRunFunnel).
  //
  // `=== false` (strict) rather than `!== true`, so an unresolved/absent
  // field never reads as eligible. This is a deliberate divergence from web,
  // which treats a missing `onboarded` as NOT onboarded and shows the funnel.
  // It is inert either way: the backend stamps a boolean on EVERY 200 body,
  // including the synthetic provisioning one (routers/container.py's
  // `_status_response` + the ensure_user_ready branch), so "ready with no
  // `onboarded`" isn't a shape any live backend produces. Erring toward
  // "don't show" is the right default for the one that would only ever come
  // from a backend older than the field.
  const eligible = !!user && !briefSubmitted && onboardedRef.current === false;

  if (eligible) return <FirstRunFunnel />;
  return <>{children}</>;
}
