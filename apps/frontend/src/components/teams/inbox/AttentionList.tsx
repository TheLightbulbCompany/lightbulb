// apps/frontend/src/components/teams/inbox/AttentionList.tsx
"use client";

import { useMemo } from "react";
import { ExternalLink } from "lucide-react";

import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import { DecisionResolver } from "@/components/teams/inbox/DecisionResolver";
import { AttachmentIssueContext } from "@/components/teams/shared/lib/attachments";
import { IssueThreadInteractionCard } from "@/components/teams/issues/IssueThreadInteractionCard";
import { useInteractionMutations } from "@/components/teams/issues/hooks/useInteractionMutations";
import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import { PanelBandEmpty } from "@/components/teams/shell";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type {
  AskUserQuestionsAnswer,
  AskUserQuestionsInteraction,
  AttentionItem,
  CompanyAgent,
  RequestCheckboxConfirmationInteraction,
  RequestConfirmationInteraction,
  SuggestTasksInteraction,
} from "@/components/teams/shared/types";

/** Past this, a waiting decision is styled as overdue rather than merely old.
 *
 *  A day is the threshold because that is the point at which the human has
 *  become the bottleneck: an agent blocked overnight has stopped being "in
 *  progress" and started being stalled. The 38-hour approval that motivated
 *  this surface would have rendered red from its second morning. */
const OVERDUE_MS = 24 * 60 * 60 * 1000;

const SOURCE_LABEL: Record<string, string> = {
  approval: "Approval",
  issue_thread_interaction: "Question",
  decision: "Decision",
  join_request: "Request",
  review: "Review",
  blocker_attention: "Blocked",
  recovery_action: "Recovery",
};

// One label per source kind; an unknown kind reads "Needs you".
function sourceLabel(item: AttentionItem): string {
  return SOURCE_LABEL[item.sourceKind] ?? "Needs you";
}

function waitedMs(item: AttentionItem): number {
  const created = Date.parse(item.createdAt);
  if (Number.isNaN(created)) return 0;
  return Math.max(0, Date.now() - created);
}

/**
 * The meta strip above every decision, identical for both entity shapes.
 *
 * A mixed list of differently-shaped things only reads as one list if every
 * row is introduced the same way. So the shape-specific body (an approval row
 * or an interaction card) always sits under the same three facts: what kind of
 * answer this is, who is waiting on it, and how long they have been waiting.
 */
function DecisionMeta({ item }: { item: AttentionItem }) {
  const waited = waitedMs(item);
  const overdue = waited >= OVERDUE_MS;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      <span
        className="rounded-sm px-1.5 py-0.5 font-medium"
        style={{ background: "var(--cream-deep)", color: "var(--midnight)" }}
      >
        {sourceLabel(item)}
      </span>
      {item.originAgentName && (
        <span style={{ color: "var(--midnight-muted)" }}>{item.originAgentName}</span>
      )}
      {item.relatedIssue?.identifier && (
        <span style={{ color: "var(--midnight-muted)" }}>
          {item.relatedIssue.identifier}
        </span>
      )}
      <span
        // The pressure cue. Honest data — the feed's own createdAt — and the
        // only place in the product that says how long a human has kept an
        // agent blocked.
        style={{ color: overdue ? "var(--err)" : "var(--midnight-muted)" }}
        title={new Date(item.createdAt).toLocaleString()}
      >
        {overdue ? "waiting " : ""}
        {timeAgo(item.createdAt)}
      </span>
    </div>
  );
}

/** An item the BFF could not hydrate — the thread is still one click away.
 *
 *  Reached when the interaction fan-out cap is hit or an upstream read failed.
 *  It states what it knows and links out; it never pretends to be resolvable. */
function UnhydratedRow({
  item,
  onOpenIssue,
}: {
  item: AttentionItem;
  onOpenIssue?: (issueId: string) => void;
}) {
  const issueId = item.relatedIssue?.id ?? null;
  return (
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm" style={{ color: "var(--midnight)" }}>
          {item.subject.title ?? sourceLabel(item)}
        </p>
        <p className="text-xs" style={{ color: "var(--midnight-muted)" }}>
          {item.whyNow}
        </p>
      </div>
      {issueId && onOpenIssue && (
        <button
          type="button"
          onClick={() => onOpenIssue(issueId)}
          className="flex items-center gap-1 text-xs underline-offset-2 hover:underline"
          style={{ color: "var(--midnight)" }}
        >
          Open thread
          <ExternalLink size={12} strokeWidth={1.75} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}

export interface AttentionListProps {
  /** Company agents, so an interaction card can name its author. */
  agents?: CompanyAgent[];
  currentUserId?: string | null;
  /** Opens an issue thread in the shell (used by unhydrated rows). */
  onOpenIssue?: (issueId: string) => void;
  /** When false, render nothing instead of the "nothing needs you" line —
   *  the caller has other rows above and that line would contradict them. */
  showEmptyState?: boolean;
}

/**
 * The unified "needs you" list — every decision waiting on this human, in one
 * server-ranked place.
 *
 * Entity types with different shapes and different verbs coexist here by
 * keeping their EXISTING components rather than being flattened into a shared
 * row: an approval renders as `ApprovalRow` (approve / reject), an interaction
 * as `IssueThreadInteractionCard` (accept / reject / respond / cancel), an
 * agent decision as `DecisionCard` (pick an option / dismiss). The first two
 * are wired to the same mutation hooks the in-thread surfaces use, so
 * resolving from this list behaves identically to resolving in place — there
 * is no second code path to keep in sync.
 *
 * DELIBERATELY NO COMMENT AFFORDANCE. Interactions default to
 * `supersedeOnUserComment: true` upstream, so a plain comment CANCELS the
 * pending card. A composer next to a decision would therefore offer the user a
 * way to silently destroy the thing they came here to answer. The card's own
 * answer inputs are resolution verbs, not comments, and are safe; nothing on
 * this surface posts a comment.
 */
export function AttentionList({
  agents = [],
  currentUserId = null,
  onOpenIssue,
  showEmptyState = true,
}: AttentionListProps) {
  const { items, isLoading, isError, refresh } = useAttentionFeed();
  const { accept, reject, respond, cancel } = useInteractionMutations();

  const agentMap = useMemo(
    () => new Map(agents.map((a) => [a.id, a])),
    [agents],
  );

  // Every resolution revalidates the issue thread (inside the mutation hook)
  // AND this feed, so the answered item leaves the list.
  const onAcceptInteraction = async (
    interaction:
      | SuggestTasksInteraction
      | RequestConfirmationInteraction
      | RequestCheckboxConfirmationInteraction,
    selectedClientKeys?: string[],
    selectedOptionIds?: string[],
  ) => {
    const body: Record<string, unknown> = {};
    if (selectedClientKeys) body.selected_client_keys = selectedClientKeys;
    if (selectedOptionIds) body.selected_option_ids = selectedOptionIds;
    await accept(interaction.issueId, interaction.id, body);
    refresh();
  };

  const onRejectInteraction = async (
    interaction:
      | SuggestTasksInteraction
      | RequestConfirmationInteraction
      | RequestCheckboxConfirmationInteraction,
    reason?: string,
  ) => {
    await reject(interaction.issueId, interaction.id, reason);
    refresh();
  };

  const onSubmitInteractionAnswers = async (
    interaction: AskUserQuestionsInteraction,
    answers: AskUserQuestionsAnswer[],
  ) => {
    await respond(interaction.issueId, interaction.id, { answers });
    refresh();
  };

  const onCancelInteraction = async (interaction: AskUserQuestionsInteraction) => {
    await cancel(interaction.issueId, interaction.id);
    refresh();
  };

  // A failed read means we cannot vouch for "nothing needs you" — which is the
  // one claim this surface exists to make. Say so instead of rendering an
  // empty list that looks like good news.
  if (isError) {
    return (
      <div
        role="alert"
        className="rounded-md border px-4 py-3 text-sm"
        style={{
          borderColor: "var(--err)",
          background: "var(--cream)",
          color: "var(--err)",
        }}
      >
        Couldn&apos;t load what needs you. Try refreshing — decisions may be
        waiting.
      </div>
    );
  }

  if (isLoading && items.length === 0) {
    return (
      <div aria-busy="true" className="flex flex-col gap-2">
        {[0, 1].map((n) => (
          <div
            key={n}
            className="h-16 animate-pulse rounded-md"
            style={{ background: "var(--cream-deep)" }}
          />
        ))}
      </div>
    );
  }

  if (items.length === 0) {
    // The caller says whether this line would be TRUE on the page it sits on:
    // a failed run rendered above is itself something that needs a human.
    if (!showEmptyState) return null;
    return <PanelBandEmpty>Nothing needs you right now.</PanelBandEmpty>;
  }

  // ONE level of depth, deliberately. The interaction card already draws its
  // own bordered surface, so the row wrapper is an unboxed group — meta strip
  // above, body below — and the shape-specific body owns the only border.
  // Boxing the wrapper too would nest a card inside a card and make the two
  // entity types sit at visibly different depths.
  const surface = { borderColor: "var(--edge)", background: "var(--cream)" };

  return (
    <ul className="flex flex-col gap-5">
      {items.map((item) => (
        <li
          key={item.id}
          // Stable handle for "how many decisions are on screen". The role
          // won't do it: an approval body is itself an <li>, so counting
          // listitems double-counts every approval.
          data-attention-kind={item.sourceKind}
          className="flex flex-col gap-2"
        >
          <DecisionMeta item={item} />
          {item.interaction ? (
            <AttachmentIssueContext.Provider value={item.interaction.issueId}>
              <IssueThreadInteractionCard
                interaction={item.interaction}
                agentMap={agentMap}
                currentUserId={currentUserId}
                onAcceptInteraction={onAcceptInteraction}
                onRejectInteraction={onRejectInteraction}
                onSubmitInteractionAnswers={onSubmitInteractionAnswers}
                onCancelInteraction={onCancelInteraction}
              />
            </AttachmentIssueContext.Provider>
          ) : item.approval ? (
            // ApprovalRow is itself an <li> (written for the band's <ul>) and
            // draws no surface of its own. Reused verbatim rather than forked,
            // so its behaviour stays in one place; the single-item <ul> is
            // what makes that valid markup, and carries the border that puts
            // an approval at the same depth as an interaction card.
            <ul className="rounded-lg border py-1" style={surface}>
              <ApprovalRow approval={item.approval} />
            </ul>
          ) : item.sourceKind === "decision" ? (
            // The feed row carries no options, so the resolver reads the full
            // decision by id and the card (which draws its own border) answers
            // it in place.
            <DecisionResolver
              key={item.subject.id}
              decisionId={item.subject.id}
              originIssue={item.relatedIssue}
              agentMap={agentMap}
              originAgentName={item.originAgentName}
            />
          ) : (
            <div className="rounded-lg border p-3" style={surface}>
              <UnhydratedRow item={item} onOpenIssue={onOpenIssue} />
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}
