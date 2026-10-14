// apps/frontend/src/components/teams/issues/IssueTreeControlSection.tsx
"use client";

// Operator safety lever: pause / resume / cancel a runaway issue subtree.
// Surfaces Paperclip's issue tree-holds (server/src/routes/issue-tree-control.ts)
// through the Teams BFF (`/teams/issues/{id}/tree-control/*`, `/tree-holds`).
// Copy is ported from upstream Paperclip's IssueDetail tree-control affordance
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).

import { useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { Textarea } from "@/components/ui/textarea";
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";

type TreeControlAction = "pause" | "resume" | "cancel";

interface ActivePauseHold {
  holdId: string;
  rootIssueId: string;
  issueId: string;
  isRoot: boolean;
  reason: string | null;
}

interface TreeControlState {
  activePauseHold: ActivePauseHold | null;
}

interface TreeControlPreviewTotals {
  totalIssues: number;
  affectedIssues: number;
  skippedIssues: number;
  activeRuns: number;
  queuedRuns: number;
  affectedAgents: number;
}

interface TreeControlPreview {
  mode: string;
  totals: TreeControlPreviewTotals;
}

const ACTION_LABEL: Record<TreeControlAction, string> = {
  pause: "Pause subtree",
  resume: "Resume subtree",
  cancel: "Cancel subtree",
};

const ACTION_HELP: Record<TreeControlAction, string> = {
  pause: "Pause active execution in this task subtree until an explicit resume.",
  resume: "Release the active subtree pause hold so held work can continue.",
  cancel:
    "Cancel non-terminal tasks in this subtree and stop queued/running work where possible.",
};

export function IssueTreeControlSection({ issueId }: { issueId: string }) {
  const { read, post } = useTeamsApi();
  const { data, error, mutate } = read<TreeControlState>(
    `/issues/${issueId}/tree-control/state`,
  );

  const [action, setAction] = useState<TreeControlAction | null>(null);
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<TreeControlPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Board-only upstream — a non-board viewer 403s. Hide the lever rather than
  // render a broken affordance.
  if (error) return null;

  const activePauseHold = data?.activePauseHold ?? null;
  const paused = activePauseHold !== null;
  const pausedAtRoot = activePauseHold?.isRoot === true;

  const openDialog = (next: TreeControlAction) => {
    setAction(next);
    setReason("");
    setPreview(null);
    setActionError(null);
    // Best-effort blast-radius preview so the operator sees the impact before
    // confirming. Failures are non-fatal — the confirm still works.
    void post<TreeControlPreview>(`/issues/${issueId}/tree-control/preview`, {
      mode: next,
    })
      .then((p) => setPreview(p))
      .catch(() => setPreview(null));
  };

  const closeDialog = () => {
    if (busy) return;
    setAction(null);
  };

  const confirm = async () => {
    if (!action) return;
    setBusy(true);
    setActionError(null);
    const trimmed = reason.trim();
    try {
      if (action === "resume") {
        // Resume RELEASES the active pause hold. The paused state is gated by
        // that hold — creating a separate resume-hold would leave the pause in
        // place, so the subtree would stay frozen and the UI stuck "paused".
        const holdId = activePauseHold?.holdId;
        if (!holdId) {
          setActionError("No active pause hold to release. Refresh and try again.");
          return;
        }
        const body: { reason?: string } = {};
        if (trimmed) body.reason = trimmed;
        await post(`/issues/${issueId}/tree-holds/${holdId}/release`, body);
      } else {
        const body: { mode: TreeControlAction; reason?: string } = { mode: action };
        if (trimmed) body.reason = trimmed;
        await post(`/issues/${issueId}/tree-holds`, body);
      }
      await mutate();
      setAction(null);
    } catch {
      setActionError("Couldn't apply the subtree control. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const totals = preview?.totals;

  return (
    <section
      className="flex flex-col gap-3 pt-4"
      style={{ borderTop: "1px solid var(--edge)" }}
    >
      <h2 className="section-label">
        <b>Subtree control</b>
      </h2>

      {paused ? (
        <div className="flex flex-col gap-2">
          <p className="text-caption" style={{ color: "var(--warn)" }}>
            {pausedAtRoot
              ? "This subtree is paused. Held work stays frozen until you resume it."
              : "Paused by an ancestor subtree hold — resume it from the root task."}
          </p>
          {pausedAtRoot && (
            <div>
              <EditorialButton
                variant="primary"
                size="sm"
                onClick={() => openDialog("resume")}
              >
                Resume subtree
              </EditorialButton>
            </div>
          )}
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <EditorialButton
            variant="secondary"
            size="sm"
            onClick={() => openDialog("pause")}
          >
            Pause subtree
          </EditorialButton>
          <EditorialButton
            variant="destructive"
            size="sm"
            onClick={() => openDialog("cancel")}
          >
            Cancel subtree
          </EditorialButton>
        </div>
      )}

      <EditorialDialog
        open={action !== null}
        onOpenChange={(open) => {
          if (!open) closeDialog();
        }}
        title={action ? ACTION_LABEL[action] : ""}
        description={action ? ACTION_HELP[action] : undefined}
        footer={
          <>
            <EditorialButton
              variant="secondary"
              size="sm"
              onClick={closeDialog}
              disabled={busy}
            >
              Cancel
            </EditorialButton>
            <EditorialButton
              variant={action === "cancel" ? "destructive" : "primary"}
              size="sm"
              onClick={() => void confirm()}
              disabled={busy}
            >
              {busy ? "Applying…" : action ? ACTION_LABEL[action] : ""}
            </EditorialButton>
          </>
        }
      >
        {totals && (
          <p className="text-ui" style={{ color: "var(--midnight-soft)" }}>
            Affects {totals.affectedIssues} of {totals.totalIssues} task
            {totals.totalIssues === 1 ? "" : "s"}, interrupting {totals.activeRuns}{" "}
            active and {totals.queuedRuns} queued run
            {totals.activeRuns + totals.queuedRuns === 1 ? "" : "s"} across{" "}
            {totals.affectedAgents} agent{totals.affectedAgents === 1 ? "" : "s"}.
          </p>
        )}
        <Textarea
          rows={2}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Reason (optional)"
          aria-label="Subtree control reason"
          disabled={busy}
          style={{
            background: "var(--cream-deep)",
            border: "1px solid var(--edge)",
            color: "var(--midnight)",
          }}
        />
        {actionError && (
          <p className="text-caption" style={{ color: "var(--err)" }}>
            {actionError}
          </p>
        )}
      </EditorialDialog>
    </section>
  );
}
