import type { IssueExecutionPolicy } from "@/components/teams/shared/types";
import {
  patchExecutionPolicy,
  stageParticipantValues,
} from "@/components/teams/issues/executionPolicy";

/** The single agent exposed by Paperclip's routine reviewer UI. Policies set
 * through the API may contain more participants; reading the first agent keeps
 * unrelated form saves lossless because the caller omits an unchanged policy. */
export function routineReviewerAgentId(
  policy: IssueExecutionPolicy | null | undefined,
): string | null {
  for (const value of stageParticipantValues(policy, "review")) {
    if (value.startsWith("agent:")) return value.slice("agent:".length) || null;
  }
  return null;
}

/** Replace only the routine's reviewer stage using the same read/merge/write
 * implementation as issue review controls. Approval stages, participant IDs,
 * and policy fields the form does not understand survive unchanged. A pure
 * reviewer policy collapses to null when the reviewer is removed. */
export function withRoutineReviewer(
  policy: IssueExecutionPolicy | null | undefined,
  reviewerAgentId: string | null,
): IssueExecutionPolicy | null {
  const desiredReviewer = reviewerAgentId ? `agent:${reviewerAgentId}` : null;
  let replacedPrimaryAgent = false;
  const reviewerValues = stageParticipantValues(policy, "review").flatMap(
    (value) => {
      // The routine UI deliberately exposes only one agent reviewer. Retain
      // every unmodeled participant so editing that one control cannot erase a
      // human reviewer or an additional agent configured elsewhere.
      if (!replacedPrimaryAgent && value.startsWith("agent:")) {
        replacedPrimaryAgent = true;
        return desiredReviewer ? [desiredReviewer] : [];
      }
      return [value];
    },
  );
  if (!replacedPrimaryAgent && desiredReviewer) reviewerValues.push(desiredReviewer);

  return patchExecutionPolicy(policy, {
    reviewers: reviewerValues,
  });
}
