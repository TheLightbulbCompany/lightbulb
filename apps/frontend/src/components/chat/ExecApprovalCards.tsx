// frontend/src/components/chat/ExecApprovalCards.tsx
/**
 * Inline exec-approval cards, stacked above ChatInput.
 *
 * Reads the global queue from `useExecApprovalsContext` and renders one
 * compact card per pending approval. No modal, no countdown — auto-prune
 * still lives in the queue hook itself.
 */

"use client";

import * as React from "react";
import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
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

export function ExecApprovalCards() {
  const { queue, resolve, error } = useExecApprovalsContext();
  if (queue.length === 0) return null;
  return (
    <div className="px-4 pt-2 pb-1 flex flex-col gap-2 max-h-[40vh] overflow-y-auto">
      {queue.map((req) => (
        <ExecApprovalCard
          key={req.id}
          pending={req}
          onDecide={(decision) => resolve(req.id, decision)}
          globalError={error}
        />
      ))}
    </div>
  );
}

interface ExecApprovalCardProps {
  pending: ExecApprovalRequest;
  onDecide: (decision: ExecApprovalDecision) => Promise<void>;
  globalError: string | null;
}

function ExecApprovalCard({ pending, onDecide, globalError }: ExecApprovalCardProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [pendingDecision, setPendingDecision] = useState<ExecApprovalDecision | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [lastFailed, setLastFailed] = useState<ExecApprovalDecision | null>(null);

  const allowsOnce = pending.allowedDecisions.includes("allow-once");
  const allowsAlways = pending.allowedDecisions.includes("allow-always");
  const allowsDeny = pending.allowedDecisions.includes("deny");

  const trustScopeLine = pending.resolvedPath
    ? `Trust will always allow ${pending.resolvedPath} on this ${HOST_LABEL[pending.host]} (any arguments).`
    : `Trust will always allow this command on this ${HOST_LABEL[pending.host]} (any arguments).`;

  const submit = async (decision: ExecApprovalDecision) => {
    setPendingDecision(decision);
    setLocalError(null);
    try {
      await onDecide(decision);
      setLastFailed(null);
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : "Couldn't send decision");
      setLastFailed(decision);
    } finally {
      setPendingDecision(null);
    }
  };

  const errorMsg = localError ?? globalError;

  return (
    <div className="rounded-md border border-[#e0dbd0] bg-[#faf7f2] p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="font-mono text-[#1a1a1a] break-all">{pending.command}</div>
        <span className="inline-flex items-center px-2 py-0.5 text-xs rounded bg-[#e8e3d9] text-[#302d28] shrink-0">
          {HOST_LABEL[pending.host]}
        </span>
      </div>
      {pending.cwd && <div className="mt-1 text-xs text-[#8a8578]">{pending.cwd}</div>}
      {pending.agentId && <div className="text-xs text-[#8a8578]">{pending.agentId}</div>}

      <div className="mt-3 flex gap-2 flex-wrap">
        <Button
          size="sm"
          variant="default"
          disabled={!allowsOnce || pendingDecision !== null}
          aria-busy={pendingDecision === "allow-once"}
          aria-label="Allow once"
          onClick={() => submit("allow-once")}
        >
          {pendingDecision === "allow-once" ? "Sending…" : "Allow once"}
        </Button>
        <Button
          size="sm"
          variant="secondary"
          disabled={!allowsAlways || pendingDecision !== null}
          aria-busy={pendingDecision === "allow-always"}
          aria-label="Trust"
          onClick={() => submit("allow-always")}
        >
          {pendingDecision === "allow-always" ? "Sending…" : "Trust"}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={!allowsDeny || pendingDecision !== null}
          aria-busy={pendingDecision === "deny"}
          aria-label="Deny"
          onClick={() => submit("deny")}
        >
          {pendingDecision === "deny" ? "Sending…" : "Deny"}
        </Button>
      </div>

      {errorMsg && (
        <div className="mt-2 text-xs text-[#b42318] flex items-center gap-2">
          <span>Couldn&apos;t send decision: {errorMsg}.</span>
          {lastFailed && (
            <button
              type="button"
              className="underline"
              onClick={() => submit(lastFailed)}
              disabled={pendingDecision !== null}
            >
              Retry
            </button>
          )}
        </div>
      )}

      <button
        type="button"
        onClick={() => setDetailsOpen((v) => !v)}
        className="mt-3 inline-flex items-center gap-1 text-xs text-[#8a8578] hover:text-[#302d28]"
      >
        {detailsOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        Details
      </button>
      {detailsOpen && (
        <div className="mt-2 space-y-1 text-xs text-[#302d28]">
          {pending.resolvedPath && (
            <div>
              <span className="text-[#8a8578]">Resolves to </span>
              <span className="font-mono">{pending.resolvedPath}</span>
            </div>
          )}
          {pending.commandArgv && (
            <div>
              <span className="text-[#8a8578]">argv </span>
              <span className="font-mono">{JSON.stringify(pending.commandArgv)}</span>
            </div>
          )}
          {pending.sessionKey && (
            <div>
              <span className="text-[#8a8578]">Session </span>
              <span className="font-mono">{pending.sessionKey}</span>
            </div>
          )}
          {allowsAlways && <div className="text-[#8a8578] pt-1">{trustScopeLine}</div>}
        </div>
      )}
    </div>
  );
}
