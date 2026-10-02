// apps/frontend/src/components/teams/inbox/DecisionResolver.tsx

// Ported from upstream Paperclip's DecisionResolver
// (paperclip/ui/src/components/DecisionResolver.tsx) (MIT, (c) 2025 Paperclip AI).
//
// Swaps vs upstream:
//   - TanStack Query -> useTeamsApi (SWR). `targetChanged` arrives on the
//     decision read itself (the BFF stamps it), not from a second open-list read.
//   - useQueries over the referenced issues -> one <IssueProbe> per id, because
//     an SWR read is a hook and the id list is dynamic. Each probe reads the
//     same `/issues/{id}` key the issue detail page uses, so the cache is shared.
//   - no cancel-tree preview: resolving every descendant is a read per issue,
//     and the card already falls back to generic copy.
//   - no mutation retry: a failed decide shows its error on the card.

"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsDetailUrl } from "@/lib/shellNav";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { decisionEffectTargetIssueIds } from "@/components/teams/shared/lib/decisions";
import { DecisionCard, type DecisionIssueRef } from "@/components/teams/inbox/DecisionCard";
import type {
  AttentionSubject,
  CompanyAgent,
  DecisionOutcome,
  Issue,
} from "@/components/teams/shared/types";

// ponytail: at most this many issue labels are looked up per card; one option
// may name 100 blockers. An unresolved id renders as "issue 1a2b3c4d". Raise
// it, or have the BFF resolve labels in one call, if real decisions hit it.
const MAX_ISSUE_LOOKUPS = 20;

/** Reads one issue for its label and reports it up. Renders nothing. */
function IssueProbe({ id, onLoaded }: { id: string; onLoaded: (id: string, issue: Issue) => void }) {
  const { read } = useTeamsApi();
  // A referenced issue may be gone; that is a label we don't have, not
  // something to keep retrying.
  const { data } = read<Issue>(teamsQueryKeys.issues.detail(id), { shouldRetryOnError: false });
  useEffect(() => {
    if (data) onLoaded(id, data);
  }, [id, data, onLoaded]);
  return null;
}

export interface DecisionResolverProps {
  decisionId: string;
  /** Origin issue subject from the attention row (already carries identifier/title). */
  originIssue?: AttentionSubject | null;
  agentMap?: Map<string, CompanyAgent>;
  /** The feed's own name for the proposer, used when the agent isn't in `agentMap`. */
  originAgentName?: string | null;
}

/**
 * Container for a decision surfaced in the attention feed. Fetches the full
 * decision, resolves the issue ids it references to labels, owns the decide /
 * dismiss calls, and revalidates the attention feed + the target issues after
 * one lands.
 */
export function DecisionResolver({ decisionId, originIssue, agentMap, originAgentName }: DecisionResolverProps) {
  const { read, post, mutate } = useTeamsApi();
  const key = teamsQueryKeys.decisions.detail(decisionId);
  const { data: decision, isLoading, error, mutate: mutateDecision } = read<DecisionOutcome>(key);

  const [busy, setBusy] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [issues, setIssues] = useState<Record<string, Issue>>({});
  const onIssueLoaded = useCallback((id: string, issue: Issue) => {
    setIssues((prev) => (prev[id] === issue ? prev : { ...prev, [id]: issue }));
  }, []);

  const originRef = useMemo<DecisionIssueRef | null>(
    () =>
      originIssue
        ? {
            id: originIssue.id,
            identifier: originIssue.identifier,
            title: originIssue.title,
            href: teamsDetailUrl("issue", originIssue.id),
            status: originIssue.status,
          }
        : null,
    [originIssue],
  );

  // Issue ids referenced by snapshots / effects / results, resolved to labels.
  // An archive proposal snapshots `attention:<kind>:<id>` keys, which are not issues.
  const referencedIds = useMemo(() => {
    if (!decision) return [] as string[];
    const ids = new Set<string>(Object.keys(decision.targetSnapshots ?? {}));
    for (const option of decision.options) {
      for (const effect of option.effects) {
        for (const id of decisionEffectTargetIssueIds(effect)) ids.add(id);
      }
    }
    for (const execution of decision.executions ?? []) {
      ids.add(execution.targetIssueId);
      const created = (execution.result ?? {}).issueId;
      if (typeof created === "string") ids.add(created);
    }
    if (originRef) ids.delete(originRef.id);
    return [...ids].filter((id) => !id.startsWith("attention:")).slice(0, MAX_ISSUE_LOOKUPS);
  }, [decision, originRef]);

  const resolveIssue = useMemo(() => {
    const map = new Map<string, DecisionIssueRef>();
    if (originRef) map.set(originRef.id, originRef);
    for (const id of referencedIds) {
      const issue = issues[id];
      if (!issue) continue;
      map.set(id, {
        id,
        identifier: issue.identifier ?? null,
        title: issue.title ?? null,
        href: teamsDetailUrl("issue", id),
        status: issue.status ?? null,
      });
    }
    return (id: string) => map.get(id) ?? null;
  }, [originRef, referencedIds, issues]);

  // decide / dismiss both return the resolved decision: seed the cache with it,
  // then re-read the feed so the answered row leaves the list.
  const resolve = async (path: string, body: unknown) => {
    setBusy(true);
    setErrorMessage(null);
    try {
      const data = await post<DecisionOutcome>(`${key}/${path}`, body);
      void mutateDecision(data, { revalidate: false });
      void mutate(`/teams${teamsQueryKeys.attention()}`);
      for (const id of Object.keys(decision?.targetSnapshots ?? {})) {
        void mutate(`/teams${teamsQueryKeys.issues.detail(id)}`);
      }
    } catch (e) {
      setErrorMessage(e instanceof Error && e.message ? e.message : "Couldn't save your answer. Try again.");
    } finally {
      setBusy(false);
    }
  };

  if (isLoading && !decision) {
    return (
      <div className="flex items-center gap-2 py-3 text-xs text-[var(--fg-mute)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading decision…
      </div>
    );
  }

  if (error || !decision) {
    return (
      <p className="py-3 text-xs text-[var(--fg-mute)]">
        This decision is no longer available — it may have been resolved elsewhere.
      </p>
    );
  }

  return (
    <>
      {referencedIds.map((id) => (
        <IssueProbe key={id} id={id} onLoaded={onIssueLoaded} />
      ))}
      <DecisionCard
        decision={decision}
        executions={decision.executions}
        targetChanged={decision.targetChanged ?? null}
        resolveIssue={resolveIssue}
        originAgentName={agentMap?.get(decision.originAgentId)?.name ?? originAgentName ?? null}
        originIssue={originRef}
        busy={busy}
        errorMessage={errorMessage}
        onDecide={(optionId, inputValues) =>
          void resolve("decide", { optionId, inputValues, idempotencyKey: crypto.randomUUID() })
        }
        onDismiss={(reason) => void resolve("dismiss", reason ? { reason } : {})}
      />
    </>
  );
}
