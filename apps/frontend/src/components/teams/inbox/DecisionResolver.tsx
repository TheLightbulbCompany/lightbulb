// apps/frontend/src/components/teams/inbox/DecisionResolver.tsx

// Ported from upstream Paperclip's DecisionResolver
// (paperclip/ui/src/components/DecisionResolver.tsx) (MIT, (c) 2025 Paperclip AI).
//
// Swaps vs upstream:
//   - TanStack Query -> useTeamsApi (SWR). As upstream, `targetChanged` comes
//     from the open list, read through one SWR key every card shares.
//   - useQueries over the referenced issues -> one <IssueProbe> per id, because
//     an SWR read is a hook and the id list is dynamic. Each probe reads the
//     same `/issues/{id}` key the issue detail page uses, so the cache is shared.
//   - no cancel-tree preview: resolving every descendant is a read per issue,
//     and the card already falls back to generic copy.
//   - no automatic mutation retry: a failed decide shows its error on the
//     card, and the person's retry reuses the same idempotency key.

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsDetailUrl } from "@/lib/shellNav";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { decisionEffectTargetIssueIds } from "@/components/teams/shared/lib/decisions";
import { DecisionCard, type DecisionIssueRef } from "@/components/teams/inbox/DecisionCard";
import type {
  AttentionSubject,
  CompanyAgent,
  DecisionListItem,
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
  const { data: decision, isLoading, mutate: mutateDecision } = read<DecisionOutcome>(key);

  // Shared across every open decision row → a single fetch carries `targetChanged`.
  const openListKey = teamsQueryKeys.decisions.list("open");
  const { data: openList, error: openListError } = read<{ decisions?: DecisionListItem[] }>(
    decision?.status === "open" ? openListKey : null,
  );
  const targetChanged = openList?.decisions?.find((entry) => entry.id === decisionId)?.targetChanged ?? null;
  // Until the list read settles we don't know which targets drifted, so the
  // card keeps strict options disabled. Once it HAS settled, a decision that
  // is not in it (past the 100-row page, or the read failed) leaves them
  // enabled: Paperclip itself skips a strict effect on a changed target at
  // decide time, and the card then shows the skipped result.
  const stalenessUnknown = decision?.status === "open" && !openList && !openListError;

  // One idempotency key per option for as long as this resolver is mounted: if
  // Paperclip applied a decide but the response was lost, the retry must replay
  // that outcome rather than hit "already resolved". Cleared only on success.
  const idempotencyKeys = useRef(new Map<string, string>());

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
  const resolve = async (path: string, body: unknown): Promise<boolean> => {
    setBusy(true);
    setErrorMessage(null);
    try {
      const data = await post<DecisionOutcome>(`${key}/${path}`, body);
      void mutateDecision(data, { revalidate: false });
      void mutate(`/teams${openListKey}`);
      void mutate(`/teams${teamsQueryKeys.attention()}`);
      for (const id of Object.keys(decision?.targetSnapshots ?? {})) {
        void mutate(`/teams${teamsQueryKeys.issues.detail(id)}`);
      }
      return true;
    } catch (e) {
      setErrorMessage(e instanceof Error && e.message ? e.message : "Couldn't save your answer. Try again.");
      // The attempt may have committed upstream and only lost its response.
      // Re-read, so that case reconciles to the resolved card instead of a
      // stale open one that 409s on retry.
      void mutateDecision();
      void mutate(`/teams${openListKey}`);
      void mutate(`/teams${teamsQueryKeys.attention()}`);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const decide = async (optionId: string, inputValues: Record<string, string>) => {
    const keys = idempotencyKeys.current;
    const idempotencyKey = keys.get(optionId) ?? crypto.randomUUID();
    keys.set(optionId, idempotencyKey);
    if (await resolve("decide", { optionId, inputValues, idempotencyKey })) keys.delete(optionId);
  };

  if (isLoading && !decision) {
    return (
      <div className="flex items-center gap-2 py-3 text-caption text-[var(--fg-mute)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading decision…
      </div>
    );
  }

  // Unavailable only when there is nothing to show. A failed revalidation keeps
  // SWR's cached decision beside `error` (the re-read after an ambiguous decide
  // can fail too): the card stays, still actionable, with its inline error.
  if (!decision) {
    return (
      <p className="py-3 text-caption text-[var(--fg-mute)]">
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
        targetChanged={targetChanged}
        stalenessUnknown={stalenessUnknown}
        resolveIssue={resolveIssue}
        originAgentName={agentMap?.get(decision.originAgentId)?.name ?? originAgentName ?? null}
        originIssue={originRef}
        busy={busy}
        errorMessage={errorMessage}
        onDecide={(optionId, inputValues) => void decide(optionId, inputValues)}
        onDismiss={(reason) => void resolve("dismiss", reason ? { reason } : {})}
      />
    </>
  );
}
