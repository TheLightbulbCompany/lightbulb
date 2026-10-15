"use client";

// An idea's approval, inside the starter's DM with the agent (spec 2026-10-14 §4.4). The agent
// files one with `request_approval` only before acting in the person's name; GET /ideas matches the
// start's newest pending one (`plan` on the running start), so the card does not depend on the
// agent pasting the link. Approve /
// Change something are the approval's own actions.
import { createContext, useEffect, useRef, useState } from "react";
import { myStartsIn, notePlanDecision, plansPollMs, useIdeas, type IdeasClient } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import type { Approval } from "@/components/teams/shared/types";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError } from "@/lib/notify";
import "@/components/ideas/ideas.css";

const PLAN_PREFIX = "Plan: ";

const payloadText = (approval: Approval, key: string) => {
  const value = approval.payload?.[key];
  return typeof value === "string" ? value : "";
};

/** A board approval titled "Plan: …": a pasted link to one renders this card (older ideas filed a
 *  plan; an idea's other approvals show in its DM from GET /ideas). */
export function isPlanApproval(approval: Approval): boolean {
  return approval.type === "request_board_approval" && payloadText(approval, "title").startsWith(PLAN_PREFIX);
}

/** The approvals the DM already shows as plan cards: a pasted link to one renders nothing more. */
export const DmPlanContext = createContext<readonly string[]>([]);

const STATUS_LINE: Record<string, string> = {
  revision_requested: "You asked for changes.",
  approved: "Approved",
  rejected: "Not approved",
  cancelled: "Withdrawn",
};

export function PlanApprovalCard({ approval, onDecided }: {
  approval: Approval;
  /** After Approve or Change something: the DM re-reads /ideas, so a revision restarts its poll. */
  onDecided?: () => void;
}) {
  const { approve, requestRevision } = useApprovalMutations();
  const [busy, setBusy] = useState(false);
  // Change something asks what, first: the agent revises from that note.
  const [note, setNote] = useState<string | null>(null);
  const run = async (fn: () => Promise<void>, fallback: string) => {
    setBusy(true);
    try { await fn(); onDecided?.(); } catch (err) { notifyApiError(err, fallback); } finally { setBusy(false); }
  };
  return <section className="plan-card" aria-label="Approval">
    <div className="plan-card__head">{payloadText(approval, "title")}</div>
    <div className="plan-card__body"><MarkdownView skipHtml>{payloadText(approval, "summary")}</MarkdownView></div>
    <div className="plan-card__foot">
      {/* Only a pending plan is decided here: after Change something the agent files a new one. */}
      {approval.status !== "pending" ? <span>{STATUS_LINE[approval.status] ?? approval.status}</span>
        : note === null ? <>
          <Button type="button" size="sm" disabled={busy}
            onClick={() => void run(() => approve(approval.id), "It could not be approved. Try again.")}>Approve</Button>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setNote("")}>Change something</Button>
        </> : <form className="plan-card__change" onSubmit={(event) => {
          event.preventDefault();
          if (note.trim()) void run(() => requestRevision(approval.id, note.trim()), "The change request did not go through. Try again.");
        }}>
          <Textarea aria-label="What should change?" placeholder="What should change?" autoFocus maxLength={2000}
            value={note} disabled={busy} onChange={(event) => setNote(event.target.value)} />
          <div className="plan-card__actions">
            <Button type="submit" size="sm" disabled={busy || !note.trim()}>Send</Button>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setNote(null)}>Cancel</Button>
          </div>
        </form>}
    </div>
  </section>;
}

function PlanById({ id, status, onDecided }: { id: string; status: string; onDecided: () => void }) {
  const { read } = useTeamsApi();
  const { data, error, mutate } = read<Approval>(`/approvals/${encodeURIComponent(id)}`);
  // The plan moved on /ideas (decided, revised): read the approval again; a failed read retries too.
  useEffect(() => { void mutate(); }, [status, mutate]);
  if (data?.id) return <PlanApprovalCard approval={data} onDecided={onDecided} />;
  if (!error) return null;
  return <section className="plan-card" aria-label="Approval">
    <div className="plan-card__foot" role="alert">
      Couldn&apos;t load the approval · <Button type="button" size="sm" variant="outline" onClick={() => void mutate()}>Retry</Button>
    </div>
  </section>;
}

/**
 * The approvals of the viewer's own ideas started in this DM, from GET /ideas, newest first: one
 * card per start. Re-reads /ideas on each new agent message here, and polls (bounded) after Start
 * and after Change something; reports the shown approvals so a pasted link to one is not drawn twice.
 */
export function DmIdeaPlan({ client, scope, roomId, userId, agentSeq, onPlan }: {
  client: IdeasClient; scope: string; roomId: string; userId: string;
  /** The newest agent message's seq in this DM: a new one may come with a new approval. */
  agentSeq: number;
  onPlan: (approvalIds: readonly string[]) => void;
}) {
  const { running, refresh } = useIdeas(client, scope, (latest) => plansPollMs(myStartsIn(latest, roomId, userId)));
  // The agent asks approval whenever it reaches an outside action, hours later too: each new
  // message from it re-reads /ideas once (the live room event the DM already gets; no polling).
  const seenSeq = useRef(agentSeq);
  useEffect(() => {
    if (agentSeq === seenSeq.current) return;
    seenSeq.current = agentSeq;
    void refresh();
  }, [agentSeq, refresh]);
  const plans = myStartsIn({ running }, roomId, userId).flatMap((start) => (start.plan ? [start.plan] : []));
  const ids = plans.map((plan) => plan.approvalId).join(",");
  useEffect(() => { onPlan(ids ? ids.split(",") : []); }, [ids, onPlan]);
  useEffect(() => () => onPlan([]), [onPlan]);
  return <>{plans.map((plan) => <PlanById key={plan.approvalId} id={plan.approvalId} status={plan.status}
    onDecided={() => { notePlanDecision(plan.approvalId); void refresh(); }} />)}</>;
}
