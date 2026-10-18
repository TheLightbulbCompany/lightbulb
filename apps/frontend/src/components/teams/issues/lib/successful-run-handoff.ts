// apps/frontend/src/components/teams/issues/lib/successful-run-handoff.ts

// Ported from upstream Paperclip's ui/src/lib/successful-run-handoff.ts
// @ v2026.1012.0-isol8.4 (MIT, (c) 2025 Paperclip AI). isSuccessfulRunHandoffRequired
// is verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared types -> components/teams/shared/types
//   - the activity/comment helpers (successfulRunHandoffFromActivity,
//     isSuccessfulRunHandoffComment, successfulRunHandoffActivityTone and their
//     constants) are not ported: their consumer is the fork's chat thread, which
//     this page does not have.

import type { Issue } from "@/components/teams/shared/types";

export function isSuccessfulRunHandoffRequired(
  issue: Pick<Issue, "successfulRunHandoff"> & Partial<Pick<Issue, "scheduledRetry">>,
) {
  const handoff = issue.successfulRunHandoff;
  if (handoff?.required !== true) return false;
  // A live continuation (running/queued run or queued wake) means an agent is
  // already on the issue — only complain when nothing is moving. The one
  // carve-out is a not-yet-promoted scheduled retry: the notice stays visible
  // there so the "Retry now" control remains reachable.
  if (!handoff.hasLiveContinuation) return true;
  return issue.scheduledRetry?.status === "scheduled_retry";
}
