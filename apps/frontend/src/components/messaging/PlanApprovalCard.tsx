"use client";

// An idea's plan, inside the starter's DM with the agent (spec 2026-10-14 §4.4). The agent files it
// with `request_approval(title="Plan: <idea>")`; GET /ideas matches it to the start (`plan` on the
// running start), so the card does not depend on the agent pasting the link. Approve plan /
// Change something are the approval's own actions.
import { createContext, useEffect, useState } from "react";
import { useIdeas, type IdeasClient, type IdeasResponse } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import type { Approval } from "@/components/teams/shared/types";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError } from "@/lib/notify";
import "@/components/ideas/ideas.css";

const PLAN_PREFIX = "Plan: ";
/** While a start in this DM has no plan yet: ask again this often, for at most this long. */
const PLAN_POLL_MS = 10_000;
const PLAN_POLL_FOR_MS = 5 * 60_000;

const payloadText = (approval: Approval, key: string) => {
  const value = approval.payload?.[key];
  return typeof value === "string" ? value : "";
};

/** A board approval titled "Plan: …" is an idea's plan (references/ideas.md in the lightbulb skill). */
export function isPlanApproval(approval: Approval): boolean {
  return approval.type === "request_board_approval" && payloadText(approval, "title").startsWith(PLAN_PREFIX);
}

/** The approval the DM already shows as its plan card: a pasted link to it renders nothing more. */
export const DmPlanContext = createContext<string | null>(null);

const STATUS_LINE: Record<string, string> = {
  revision_requested: "You asked for changes. The plan is being revised.",
  approved: "Plan approved",
  rejected: "Plan not approved",
  cancelled: "Plan withdrawn",
};

export function PlanApprovalCard({ approval }: { approval: Approval }) {
  const { approve, requestRevision } = useApprovalMutations();
  const [busy, setBusy] = useState(false);
  const run = async (fn: (id: string) => Promise<void>, fallback: string) => {
    setBusy(true);
    try { await fn(approval.id); } catch (err) { notifyApiError(err, fallback); } finally { setBusy(false); }
  };
  return <section className="plan-card" aria-label="Plan for approval">
    <div className="plan-card__head">{payloadText(approval, "title")}</div>
    <div className="plan-card__body"><MarkdownView skipHtml>{payloadText(approval, "summary")}</MarkdownView></div>
    <div className="plan-card__foot">
      {/* Only a pending plan is decided here: after Change something the agent files a new one. */}
      {approval.status === "pending" ? <>
        <Button type="button" size="sm" disabled={busy} onClick={() => void run(approve, "The plan could not be approved. Try again.")}>Approve plan</Button>
        <Button type="button" size="sm" variant="outline" disabled={busy}
          onClick={() => void run(requestRevision, "The change request did not go through. Try again.")}>Change something</Button>
      </> : <span>{STATUS_LINE[approval.status] ?? approval.status}</span>}
    </div>
  </section>;
}

function PlanById({ id }: { id: string }) {
  const { read } = useTeamsApi();
  const { data } = read<Approval>(`/approvals/${encodeURIComponent(id)}`);
  return data?.id ? <PlanApprovalCard approval={data} /> : null;
}

/**
 * The plan of the viewer's own idea started in this DM, from GET /ideas. Polls (bounded) while
 * the start has no plan yet; reports the shown approval so a pasted link to it is not drawn twice.
 */
export function DmIdeaPlan({ client, scope, roomId, userId, onPlan }: {
  client: IdeasClient; scope: string; roomId: string; userId: string; onPlan: (approvalId: string | null) => void;
}) {
  const mineIn = (data: IdeasResponse | undefined) =>
    data?.running.find((r) => r.roomId === roomId && r.starterId === userId);
  const { running } = useIdeas(client, scope, (latest) => {
    const mine = mineIn(latest);
    // The window runs from the Start, not from opening the DM: an old DM never polls again.
    const started = Date.parse(mine?.createdAt ?? "");
    return mine && !mine.plan && Date.now() - started < PLAN_POLL_FOR_MS ? PLAN_POLL_MS : 0;
  });
  const approvalId = mineIn({ ideas: [], running })?.plan?.approvalId ?? null;
  useEffect(() => { onPlan(approvalId); }, [approvalId, onPlan]);
  useEffect(() => () => onPlan(null), [onPlan]);
  return approvalId ? <PlanById id={approvalId} /> : null;
}
