// frontend/src/components/chat/ExecApprovalDialog.tsx
/**
 * Modal exec-approval dialog.
 *
 * Mirrors OpenClaw's native rendering at
 * ~/Desktop/openclaw/ui/src/ui/views/exec-approval.ts. Renders the head of
 * the global queue from `useExecApprovalsContext` as a focused modal so it
 * is always reachable, regardless of where in the chat the underlying tool
 * call lives or whether that bubble has scrolled offscreen.
 *
 * Behavior parity with OpenClaw:
 *   - Always shows queue[0] (FIFO).
 *   - Live countdown to expiry.
 *   - Three buttons (Allow Once / Always Allow / Deny) — only those the
 *     server explicitly allowed render.
 *   - Auto-deny on dialog cancel (Escape / outside click) when the server
 *     allows deny — otherwise dismissal is blocked so we can't drop the
 *     approval on the floor.
 *   - Queue counter badge when more than one is pending.
 */

"use client";

import * as React from "react";
import { useState, useEffect } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  useExecApprovalsContext,
  type ExecApprovalDecision,
  type ExecApprovalRequest,
} from "@/hooks/useExecApprovals";

const HOST_LABEL: Record<ExecApprovalRequest["host"], string> = {
  gateway: "container",
  node: "node",
  sandbox: "sandbox",
};

function formatRemaining(ms: number): string {
  if (ms <= 0) return "expired";
  const totalSec = Math.ceil(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return sec === 0 ? `${min}m` : `${min}m ${sec}s`;
}

function useCountdown(expiresAtMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return Math.max(0, expiresAtMs - now);
}

export function ExecApprovalDialog() {
  const { queue, resolve, error } = useExecApprovalsContext();
  const active = queue[0];
  const [busyDecision, setBusyDecision] = useState<ExecApprovalDecision | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);

  // Reset details state when the head of queue changes — a new approval
  // shouldn't inherit the expanded state of the previous one.
  useEffect(() => {
    setDetailsOpen(false);
    setBusyDecision(null);
  }, [active?.id]);

  if (!active) return null;

  return <ActiveDialog
    key={active.id}
    active={active}
    queueCount={queue.length}
    busyDecision={busyDecision}
    setBusyDecision={setBusyDecision}
    detailsOpen={detailsOpen}
    setDetailsOpen={setDetailsOpen}
    error={error}
    resolve={resolve}
  />;
}

interface ActiveDialogProps {
  active: ExecApprovalRequest;
  queueCount: number;
  busyDecision: ExecApprovalDecision | null;
  setBusyDecision: (d: ExecApprovalDecision | null) => void;
  detailsOpen: boolean;
  setDetailsOpen: (v: boolean) => void;
  error: string | null;
  resolve: (id: string, decision: ExecApprovalDecision) => Promise<void>;
}

function ActiveDialog({
  active,
  queueCount,
  busyDecision,
  setBusyDecision,
  detailsOpen,
  setDetailsOpen,
  error,
  resolve,
}: ActiveDialogProps) {
  const remainingMs = useCountdown(active.expiresAtMs);
  const allowsOnce = active.allowedDecisions.includes("allow-once");
  const allowsAlways = active.allowedDecisions.includes("allow-always");
  const allowsDeny = active.allowedDecisions.includes("deny");
  const expired = remainingMs <= 0;

  const submit = async (decision: ExecApprovalDecision) => {
    setBusyDecision(decision);
    try {
      await resolve(active.id, decision);
    } catch {
      // Error surfaces in `error` from context; re-enable buttons so the
      // user can retry or pick a different decision.
    } finally {
      setBusyDecision(null);
    }
  };

  // Auto-deny on dismissal when allowed; otherwise block dismissal so the
  // approval can't be silently dropped.
  const onOpenChange = (open: boolean) => {
    if (open || busyDecision !== null) return;
    if (allowsDeny && !expired) {
      void submit("deny");
    }
    // If deny isn't allowed, swallow the close — the user must pick an
    // explicit decision.
  };

  const trustScopeLine = active.resolvedPath
    ? `Trust will always allow ${active.resolvedPath} on this ${HOST_LABEL[active.host]} (any arguments).`
    : `Trust will always allow this command on this ${HOST_LABEL[active.host]} (any arguments).`;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center justify-between gap-3">
            <span>Approval needed</span>
            {queueCount > 1 && (
              <span className="text-xs font-normal text-[#8a8578] bg-[#f3efe6] rounded-full px-2 py-0.5">
                {queueCount - 1} more pending
              </span>
            )}
          </DialogTitle>
          <DialogDescription>
            {expired ? "Expired" : `Expires in ${formatRemaining(remainingMs)}`}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="rounded-md border border-[#e0dbd0] bg-[#faf7f2] p-2.5 font-mono text-xs break-all">
            {active.command}
          </div>

          <div className="text-xs text-[#6b6b6b]">
            Will run on the <span className="font-medium">{HOST_LABEL[active.host]}</span>
            {active.cwd ? <> in <span className="font-mono">{active.cwd}</span></> : null}.
          </div>

          {allowsAlways && (
            <div className="text-xs text-[#8a8578] italic">{trustScopeLine}</div>
          )}

          <button
            type="button"
            onClick={() => setDetailsOpen(!detailsOpen)}
            className="flex items-center gap-1 text-xs text-[#6b6b6b] hover:text-[#302d28]"
          >
            {detailsOpen ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            Details
          </button>
          {detailsOpen && (
            <div className="rounded-md border border-[#e0dbd0] bg-[#faf7f2] p-2.5 text-xs space-y-1">
              {active.resolvedPath && (
                <div>
                  <span className="font-medium">Resolved path:</span>{" "}
                  <span className="font-mono break-all">{active.resolvedPath}</span>
                </div>
              )}
              {active.commandArgv && (
                <div>
                  <span className="font-medium">Argv:</span>{" "}
                  <span className="font-mono break-all">
                    {JSON.stringify(active.commandArgv)}
                  </span>
                </div>
              )}
              {active.agentId && (
                <div>
                  <span className="font-medium">Agent:</span>{" "}
                  <span className="font-mono">{active.agentId}</span>
                </div>
              )}
              {active.sessionKey && (
                <div>
                  <span className="font-medium">Session:</span>{" "}
                  <span className="font-mono break-all">{active.sessionKey}</span>
                </div>
              )}
              <div>
                <span className="font-medium">Approval ID:</span>{" "}
                <span className="font-mono">{active.id}</span>
              </div>
            </div>
          )}

          {error && (
            <div className="rounded-md border border-[#f1c0c0] bg-[#fdecec] p-2 text-xs text-[#8a1f1f]">
              {error}
            </div>
          )}
        </div>

        <div className="flex flex-wrap gap-2 justify-end">
          {allowsDeny && (
            <Button
              variant="outline"
              size="sm"
              disabled={busyDecision !== null || expired}
              onClick={() => submit("deny")}
              className="border-[#e0dbd0] text-[#8a1f1f] hover:bg-[#fdecec]"
            >
              {busyDecision === "deny" ? "Denying…" : "Deny"}
            </Button>
          )}
          {allowsAlways && (
            <Button
              variant="outline"
              size="sm"
              disabled={busyDecision !== null || expired}
              onClick={() => submit("allow-always")}
            >
              {busyDecision === "allow-always" ? "Trusting…" : "Always allow"}
            </Button>
          )}
          {allowsOnce && (
            <Button
              size="sm"
              disabled={busyDecision !== null || expired}
              onClick={() => submit("allow-once")}
            >
              {busyDecision === "allow-once" ? "Allowing…" : "Allow once"}
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
