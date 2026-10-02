// apps/frontend/src/components/teams/shared/lib/decisions.ts

// Ported from upstream Paperclip's decisionEffectTargetIssueIds
// (paperclip/packages/shared/src/types/decision.ts) (MIT, (c) 2025 Paperclip AI).

import type { DecisionEffect } from "@/components/teams/shared/types";

/** Every issue one effect touches: its target, plus the parent / blockers it names. */
export function decisionEffectTargetIssueIds(effect: DecisionEffect): string[] {
  const ids = new Set([effect.targetIssueId]);
  if (effect.type === "create_issue") {
    if (effect.draft.parentId) ids.add(effect.draft.parentId);
    for (const id of effect.draft.blockedByIssueIds ?? []) ids.add(id);
  }
  if (effect.type === "resolve_blocker") {
    for (const id of effect.removeBlockedByIssueIds) ids.add(id);
  }
  return [...ids];
}
