"use client";

// An idea's plan, inside the DM where the agent posted it (spec 2026-10-14 §4.4). The agent files
// it with `request_approval(title="Plan: <idea>")` and posts the approval's link; RoomAppLinks
// resolves that link to this card. Approve plan / Change something are the approval's own actions.
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import type { Approval } from "@/components/teams/shared/types";
import { notifyApiError } from "@/lib/notify";
import "@/components/ideas/ideas.css";

const PLAN_PREFIX = "Plan: ";

const payloadText = (approval: Approval, key: string) => {
  const value = approval.payload?.[key];
  return typeof value === "string" ? value : "";
};

/** A board approval titled "Plan: …" is an idea's plan (references/ideas.md in the lightbulb skill). */
export function isPlanApproval(approval: Approval): boolean {
  return approval.type === "request_board_approval" && payloadText(approval, "title").startsWith(PLAN_PREFIX);
}

const SETTLED: Record<string, string> = { approved: "Plan approved", rejected: "Plan not approved", cancelled: "Plan withdrawn" };

export function PlanApprovalCard({ approval }: { approval: Approval }) {
  const { approve, requestRevision } = useApprovalMutations();
  const [busy, setBusy] = useState(false);
  const run = async (fn: (id: string) => Promise<void>, fallback: string) => {
    setBusy(true);
    try { await fn(approval.id); } catch (err) { notifyApiError(err, fallback); } finally { setBusy(false); }
  };
  const actionable = approval.status === "pending" || approval.status === "revision_requested";
  return <section className="plan-card" aria-label="Plan for approval">
    <div className="plan-card__head">{payloadText(approval, "title")}</div>
    <div className="plan-card__body"><MarkdownView skipHtml>{payloadText(approval, "summary")}</MarkdownView></div>
    <div className="plan-card__foot">
      {actionable ? <>
        <Button type="button" size="sm" disabled={busy} onClick={() => void run(approve, "The plan could not be approved. Try again.")}>Approve plan</Button>
        {approval.status === "pending"
          ? <Button type="button" size="sm" variant="outline" disabled={busy}
            onClick={() => void run(requestRevision, "The change request did not go through. Try again.")}>Change something</Button>
          : <span>You asked for changes. Say what to change below.</span>}
      </> : <span>{SETTLED[approval.status] ?? approval.status}</span>}
    </div>
  </section>;
}
