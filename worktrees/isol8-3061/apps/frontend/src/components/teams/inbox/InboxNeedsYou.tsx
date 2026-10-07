"use client";

// Inbox section 1 — everything waiting on a human, in one band.
//
// Two disjoint feeds render here on purpose. `useAttentionFeed` is the
// server-ranked decision queue (approvals, agent questions, blocked, recovery)
// and deliberately EXCLUDES failed runs, which used to live in their own
// "Agent failures" band. That band is gone, so its feed (`useInboxTriage`,
// dismissed locally via `useAcknowledgedRuns`) renders here too — otherwise a
// failed run would have nowhere left to be seen.

import { AttentionList } from "@/components/teams/inbox/AttentionList";
import { FailedRunRow } from "@/components/teams/inbox/FailedRunRow";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";
import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";
import { groupFailedRuns } from "@/components/teams/inbox/lib/groupFailedRuns";
import { PanelBand, PanelBandEmpty } from "@/components/teams/shell";
import type { CompanyAgent } from "@/components/teams/shared/types";

export function InboxNeedsYou({
  companyId,
  agents,
  currentUserId = null,
  onOpenIssue,
}: {
  companyId: string;
  agents: CompanyAgent[];
  currentUserId?: string | null;
  onOpenIssue?: (issueId: string) => void;
}) {
  const { acknowledgedRunIds, ack } = useAcknowledgedRuns(companyId);
  const { failedRuns, isError: runsError } =
    useInboxTriage(acknowledgedRunIds);
  const { count: decisionCount } = useAttentionFeed();

  const groups = groupFailedRuns(failedRuns);
  const agentName = (id: string | null | undefined) =>
    agents.find((a) => a.id === id)?.name ?? "An agent";
  // `groups.length`, NOT the raw occurrence count: a flapping heartbeat emits
  // the same failure once per occurrence (492 identical rows on prod once),
  // and the band collapses those into ONE row reading "×492". Counting the raw
  // total here would put "492 waiting" beside that single row — the exact
  // inflation the grouping exists to prevent, moved from the list into the
  // header.
  const waiting = decisionCount + groups.length;

  return (
    <PanelBand
      id="inbox-needs-you"
      label="Needs you"
      right={waiting > 0 ? <span>{waiting} waiting</span> : undefined}
    >
      {/* When the failed-run read fails we cannot vouch for "nothing needs
          you", so the all-clear below is suppressed rather than printed under
          its own alert. */}
      {runsError && (
        <PanelBandEmpty role="alert">Couldn&apos;t check for failed runs just now.</PanelBandEmpty>
      )}
      {groups.length > 0 && (
        <ul className="flex flex-col">
          {groups.map((group) => (
            <FailedRunRow
              key={group.key}
              run={group.representative}
              agentName={agentName(group.representative.agentId)}
              count={group.count}
              // Dismissing a grouped row must ack EVERY member id, or the
              // unacked occurrences re-populate the band on the next read.
              onDismiss={() => group.runs.forEach((r) => ack(r.id))}
            />
          ))}
        </ul>
      )}
      <AttentionList
        agents={agents}
        currentUserId={currentUserId}
        onOpenIssue={onOpenIssue}
        showEmptyState={groups.length === 0 && !runsError}
      />
    </PanelBand>
  );
}
