// apps/frontend/src/components/teams/approvals/ApprovalDetail.tsx

// Ported from upstream Paperclip's ApprovalDetail page
// (paperclip/ui/src/pages/ApprovalDetail.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Layout, copy, the action matrix (approve /
// reject / request revision / mark resubmitted / delete disapproved agent),
// linked tasks and the comments thread are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - react-query + approvalsApi/agentsApi -> useTeamsApi reads and the Teams BFF
//                                       (/teams/approvals/{id}[/comments|/issues|
//                                       /request-revision|/resubmit]), each fenced
//                                       to the caller's company server-side
//   - route params / breadcrumbs / company sync -> a detail panel prop; the
//                                       shell owns the tab title, the BFF the company
//   - navigate("?resolved=approved")  -> local state (a panel has no own URL)
//   - react-router Link               -> in-shell anchors; agents -> the Agents
//                                       panel, /approvals -> the Inbox
//   - Reject asks for a reason first: the BFF's reject body requires one
//   - the budget stop's "/costs" link is dropped: Lightbulb has no budget page
//   - Lightbulb's own cards (payload.isol8_action) keep their inbox actions,
//     rendered by ApprovalPayloadRenderer, so the generic action row is hidden
//     for them the way upstream hides it for budget stops
//   - StatusBadge / PageSkeleton      -> a status pill / the shared PageSkeleton
//   - green/destructive palette       -> brand ok token (destructive already is one)

"use client";

import { useMemo, useState } from "react";
import { CheckCircle2, ChevronRight, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, navigateShell, teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { Identity } from "@/components/teams/shared/components/Identity";
import { TaskMarkdown } from "@/components/teams/shared/components/TaskMarkdown";
import { PageSkeleton } from "@/components/teams/shared/components/PageSkeleton";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import {
  approvalLabel,
  typeIcon,
  defaultTypeIcon,
  isLightbulbActionApproval,
  ApprovalPayloadRenderer,
} from "./ApprovalPayload";
import type {
  Approval,
  ApprovalComment,
  ApprovalDetail as ApprovalRecord,
  CompanyAgent,
  Issue,
} from "@/components/teams/shared/types";

/** Upstream's react-router Link, as an in-shell anchor (new-tab still works). */
function Link({ to, className, children }: { to: string; className?: string; children: React.ReactNode }) {
  return (
    <a href={to} className={className} onClick={(e) => handleShellLinkClick(e, to)}>
      {children}
    </a>
  );
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span className="inline-flex items-center rounded-full border border-border/70 px-2.5 py-0.5 text-caption capitalize text-muted-foreground">
      {status.replace(/_/g, " ")}
    </span>
  );
}

function asList<T>(data: unknown, key: string): T[] | undefined {
  if (Array.isArray(data)) return data as T[];
  const inner = (data as Record<string, unknown> | undefined)?.[key];
  return Array.isArray(inner) ? (inner as T[]) : undefined;
}

export function ApprovalDetail({ approvalId }: { approvalId: string }) {
  const { read, post } = useTeamsApi();
  const { approve, reject } = useApprovalMutations();
  const { remove: removeAgent } = useAgentMutations();
  const [commentBody, setCommentBody] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [showRawPayload, setShowRawPayload] = useState(false);
  const [showApprovedBanner, setShowApprovedBanner] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [pending, setPending] = useState<string | null>(null);

  const approvalRead = read<ApprovalRecord>(`/approvals/${approvalId}`);
  const commentsRead = read<unknown>(`/approvals/${approvalId}/comments`);
  const issuesRead = read<unknown>(`/approvals/${approvalId}/issues`);
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const approval = approvalRead.data;
  const comments = asList<ApprovalComment>(commentsRead.data, "comments");
  const linkedIssues = asList<Issue>(issuesRead.data, "issues");

  const agentNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const agent of agentsData?.agents ?? []) map.set(agent.id, agent.name);
    return map;
  }, [agentsData]);

  const refresh = () => {
    void approvalRead.mutate();
    void commentsRead.mutate();
    void issuesRead.mutate();
  };

  async function run(key: string, fn: () => Promise<unknown>, fallback: string, onSuccess?: () => void) {
    setPending(key);
    try {
      await fn();
      setError(null);
      refresh();
      onSuccess?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : fallback);
    } finally {
      setPending(null);
    }
  }

  if (approvalRead.isLoading) return <div className="p-8"><PageSkeleton /></div>;
  if (!approval) return <p className="p-8 text-ui text-muted-foreground">Approval not found.</p>;

  const payload = (approval.payload ?? {}) as Record<string, unknown>;
  const type = approval.type ?? "";
  const linkedAgentId = typeof payload.agentId === "string" ? payload.agentId : null;
  const isLightbulbAction = isLightbulbActionApproval(payload);
  const isActionable = approval.status === "pending" || approval.status === "revision_requested";
  const isBudgetApproval = type === "budget_override_required";
  const TypeIcon = typeIcon[type] ?? defaultTypeIcon;
  const primaryLinkedIssue = linkedIssues?.[0] ?? null;
  const resolvedCta =
    primaryLinkedIssue
      ? {
          label:
            (linkedIssues?.length ?? 0) > 1
              ? "Review linked tasks"
              : "Review linked task",
          to: teamsDetailUrl("issue", primaryLinkedIssue.identifier ?? primaryLinkedIssue.id),
        }
      : linkedAgentId
        ? {
            label: "Open hired agent",
            to: teamsPanelUrl("agents"),
          }
        : {
            label: "Back to approvals",
            to: teamsPanelUrl("inbox"),
          };

  return (
    <div className="space-y-6 max-w-3xl p-6">
      {showApprovedBanner && approval.status === "approved" && (
        <div className="border border-ok/40 bg-ok/10 rounded-lg px-4 py-3 animate-in fade-in zoom-in-95 duration-300">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-start gap-2">
              <div className="relative mt-0.5">
                <CheckCircle2 className="h-4 w-4 text-ok" />
                <Sparkles className="h-3 w-3 text-ok absolute -right-2 -top-1 animate-pulse" />
              </div>
              <div>
                <p className="text-ui text-foreground font-medium">Approval confirmed</p>
                <p className="text-caption text-muted-foreground">
                  Requesting agent was notified to review this approval and linked tasks.
                </p>
              </div>
            </div>
            <Button
              size="sm"
              variant="outline"
              className="border-ok/50 text-ok hover:bg-ok/10"
              onClick={() => navigateShell(resolvedCta.to)}
            >
              {resolvedCta.label}
            </Button>
          </div>
        </div>
      )}
      <div className="border border-border rounded-lg p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <TypeIcon className="h-5 w-5 text-muted-foreground shrink-0" />
            <div>
              <h2 className="text-lg font-semibold">{approvalLabel(type, payload)}</h2>
              <p className="text-caption text-muted-foreground font-mono">{approval.id}</p>
            </div>
          </div>
          <StatusBadge status={approval.status} />
        </div>
        <div className="text-ui space-y-1">
          {approval.requestedByAgentId && (
            <div className="flex items-center gap-2">
              <span className="text-muted-foreground text-caption">Requested by</span>
              <Identity
                name={agentNameById.get(approval.requestedByAgentId) ?? approval.requestedByAgentId.slice(0, 8)}
                size="sm"
              />
            </div>
          )}
          <ApprovalPayloadRenderer type={type} payload={payload} approval={approval as Approval} />
          <button
            type="button"
            className="flex items-center gap-1 text-caption text-muted-foreground hover:text-foreground transition-colors mt-2"
            onClick={() => setShowRawPayload((v) => !v)}
          >
            <ChevronRight className={`h-3 w-3 transition-transform ${showRawPayload ? "rotate-90" : ""}`} />
            See full request
          </button>
          {showRawPayload && (
            <pre className="text-caption bg-muted/40 rounded-md p-3 overflow-x-auto">
              {JSON.stringify(payload, null, 2)}
            </pre>
          )}
          {approval.decisionNote && (
            <p className="text-caption text-muted-foreground">Decision note: {approval.decisionNote}</p>
          )}
        </div>
        {error && <p className="text-ui text-destructive">{error}</p>}
        {linkedIssues && linkedIssues.length > 0 && (
          <div className="pt-2 border-t border-border/60">
            <p className="text-caption text-muted-foreground mb-1.5">Linked Tasks</p>
            <div className="space-y-1.5">
              {linkedIssues.map((issue) => (
                <Link
                  key={issue.id}
                  to={teamsDetailUrl("issue", issue.identifier ?? issue.id)}
                  className="block text-caption rounded border border-border/70 px-2 py-1.5 hover:bg-accent/20"
                >
                  <span className="font-mono text-muted-foreground mr-2">
                    {issue.identifier ?? issue.id.slice(0, 8)}
                  </span>
                  <span>{issue.title}</span>
                </Link>
              ))}
            </div>
            <p className="text-caption text-muted-foreground mt-2">
              Linked tasks remain open until the requesting agent follows up and closes them.
            </p>
          </div>
        )}
        {!isLightbulbAction && (
          <div className="flex flex-wrap items-center gap-2">
            {isActionable && !isBudgetApproval && !rejecting && (
              <>
                <Button
                  size="sm"
                  className="bg-ok hover:bg-ok/90 text-background"
                  onClick={() =>
                    run("approve", () => approve(approval.id), "Approve failed", () => setShowApprovedBanner(true))
                  }
                  disabled={pending === "approve"}
                >
                  Approve
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => setRejecting(true)}
                  disabled={pending === "reject"}
                >
                  Reject
                </Button>
              </>
            )}
            {isActionable && !isBudgetApproval && rejecting && (
              <div className="flex w-full items-center gap-2">
                <Textarea
                  aria-label="Reason for rejection"
                  placeholder="Reason for rejection"
                  value={rejectReason}
                  onChange={(e) => setRejectReason(e.target.value)}
                  rows={1}
                  className="min-w-0 flex-1"
                />
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() =>
                    run("reject", () => reject(approval.id, rejectReason.trim()), "Reject failed", () => {
                      setRejecting(false);
                      setRejectReason("");
                    })
                  }
                  disabled={!rejectReason.trim() || pending === "reject"}
                >
                  Confirm reject
                </Button>
                <Button size="sm" variant="outline" onClick={() => setRejecting(false)}>
                  Cancel
                </Button>
              </div>
            )}
            {isBudgetApproval && approval.status === "pending" && (
              <p className="text-ui text-muted-foreground">
                Resolve this budget stop from the budget controls.
              </p>
            )}
            {approval.status === "pending" && (
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  run("revision", () => post(`/approvals/${approval.id}/request-revision`, {}), "Revision request failed")
                }
                disabled={pending === "revision"}
              >
                Request revision
              </Button>
            )}
            {approval.status === "revision_requested" && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => run("resubmit", () => post(`/approvals/${approval.id}/resubmit`, {}), "Resubmit failed")}
                disabled={pending === "resubmit"}
              >
                Mark resubmitted
              </Button>
            )}
            {approval.status === "rejected" && type === "hire_agent" && linkedAgentId && (
              <Button
                size="sm"
                variant="outline"
                className="text-destructive border-destructive/40"
                onClick={() => {
                  if (!window.confirm("Delete this disapproved agent? This cannot be undone.")) return;
                  void run("delete", () => removeAgent(linkedAgentId), "Delete failed", () =>
                    navigateShell(teamsPanelUrl("inbox")),
                  );
                }}
                disabled={pending === "delete"}
              >
                Delete disapproved agent
              </Button>
            )}
          </div>
        )}
      </div>

      <div className="border border-border rounded-lg p-4 space-y-3">
        <h3 className="text-ui font-medium">Comments ({comments?.length ?? 0})</h3>
        <div className="space-y-2">
          {(comments ?? []).map((comment) => (
            <div key={comment.id} className="border border-border/60 rounded-md p-3">
              <div className="flex items-center justify-between mb-1">
                {comment.authorAgentId ? (
                  <Link to={teamsPanelUrl("agents")} className="hover:underline">
                    <Identity
                      name={agentNameById.get(comment.authorAgentId) ?? comment.authorAgentId.slice(0, 8)}
                      size="sm"
                    />
                  </Link>
                ) : (
                  <Identity name="Board" size="sm" />
                )}
                <span className="text-caption text-muted-foreground">
                  {new Date(comment.createdAt).toLocaleString()}
                </span>
              </div>
              <div className="text-ui">
                <TaskMarkdown body={comment.body} />
              </div>
            </div>
          ))}
        </div>
        <Textarea
          value={commentBody}
          onChange={(e) => setCommentBody(e.target.value)}
          placeholder="Add a comment..."
          rows={3}
        />
        <div className="flex justify-end">
          <Button
            size="sm"
            onClick={() =>
              run(
                "comment",
                () => post(`/approvals/${approval.id}/comments`, { body: commentBody.trim() }),
                "Comment failed",
                () => setCommentBody(""),
              )
            }
            disabled={!commentBody.trim() || pending === "comment"}
          >
            {pending === "comment" ? "Posting…" : "Post comment"}
          </Button>
        </div>
      </div>
    </div>
  );
}
