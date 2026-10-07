/**
 * Pure helper for the `onboarding_step_completed` analytics gating.
 *
 * Extracted from ProvisioningStepper so the gating logic can be unit-
 * tested without mounting the full stepper (which depends on Clerk +
 * billing + container hooks).
 *
 * Rules:
 * - prev must be in the active step list (so we have a meaningful step_index)
 * - if curr is also in the list, only emit on forward moves (currIdx > prevIdx)
 * - if curr is NOT in the list (e.g. "channels", which is a wizard outside
 *   the stepper rows), treat it as "advanced past the list" — emit
 *
 * Backward transitions (e.g. billing settles late and bounces phase from
 * channels back to payment) must NOT emit. That case has prev not in list
 * → first guard returns null.
 */

export interface StepDescriptor<P extends string> {
  phase: P;
}

export interface OnboardingCompletionPayload<P extends string> {
  step_name: P;
  step_index: number;
}

export function nextOnboardingCompletion<P extends string>(
  prev: P,
  curr: P,
  stepList: ReadonlyArray<StepDescriptor<P>>,
): OnboardingCompletionPayload<P> | null {
  const prevIdx = stepList.findIndex((s) => s.phase === prev);
  if (prevIdx < 0) return null;
  const currIdx = stepList.findIndex((s) => s.phase === curr);
  if (currIdx >= 0 && currIdx <= prevIdx) return null;
  return { step_name: prev, step_index: prevIdx };
}
