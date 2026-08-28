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
import { Button } from "@/components/ui/button";
import {
  useExecApprovalsContext,
  type ExecApprovalDecision,
  type ExecApprovalRequest,
} from "@/hooks/useExecApprovals";
import { formatApprovalDisplayPath } from "@/lib/approvalDisplayPath";
import { notifyApiError } from "@/lib/notify";

const HOST_LABEL: Record<ExecApprovalRequest["host"], string> = {
  gateway: "container",
  node: "node",
  sandbox: "sandbox",
};

/** Render the command with executable spans wrapped in <mark>. Ported
 *  from upstream renderCommandWithSpans: overlap-resolve the spans, then
 *  interleave plain slices with marks. Falls back to plain text when
 *  nothing valid remains. */
function CommandWithSpans({
  command,
  spans,
}: {
  command: string;
  spans?: { startIndex: number; endIndex: number }[];
}) {
  const valid = [...(spans ?? [])]
    .filter(
      (s) =>
        Number.isSafeInteger(s.startIndex) &&
        Number.isSafeInteger(s.endIndex) &&
        s.startIndex >= 0 &&
        s.endIndex > s.startIndex &&
        s.endIndex <= command.length,
    )
    .sort((a, b) => a.startIndex - b.startIndex || b.endIndex - a.endIndex);
  const accepted: { startIndex: number; endIndex: number }[] = [];
  let cursor = 0;
  for (const s of valid) {
    if (s.startIndex < cursor) continue;
    accepted.push(s);
    cursor = s.endIndex;
  }
  if (accepted.length === 0) {
    return <div className="font-mono text-[var(--midnight)] break-all">{command}</div>;
  }
  const parts: React.ReactNode[] = [];
  cursor = 0;
  accepted.forEach((s, i) => {
    if (s.startIndex > cursor) {
      parts.push(<span key={`p${i}`}>{command.slice(cursor, s.startIndex)}</span>);
    }
    parts.push(
      <mark key={`m${i}`} className="bg-bulb text-[var(--midnight)] rounded px-0.5">
        {command.slice(s.startIndex, s.endIndex)}
      </mark>,
    );
    cursor = s.endIndex;
  });
  if (cursor < command.length) {
    parts.push(<span key="tail">{command.slice(cursor)}</span>);
  }
  return <div className="font-mono text-[var(--midnight)] break-all">{parts}</div>;
}

/** One label/value meta row. `path` opt compacts home dirs to ~. Ported
 *  from upstream renderMetaRow. */
function MetaRow({
  label,
  value,
  path,
}: {
  label: string;
  value?: string | null;
  path?: boolean;
}) {
  if (!value) return null;
  return (
    <div className="flex justify-between gap-3">
      <span className="text-[var(--midnight-muted)]">{label}</span>
      <span className="font-mono text-right break-all">
        {path ? formatApprovalDisplayPath(value) : value}
      </span>
    </div>
  );
}

/** Plugin-approval body: free-text description + a plugin-flavored meta
 *  block. Ported from upstream renderPluginBody. The exec command-span +
 *  exec-only meta rows (Host/CWD/Security/Ask/argv) are intentionally
 *  absent — they're not meaningful for a plugin/MCP tool call. */
function PluginBody({ pending }: { pending: ExecApprovalRequest }) {
  return (
    <>
      {pending.pluginDescription && (
        <pre className="font-mono text-[var(--midnight)] whitespace-pre-wrap break-words text-sm mt-1">
          {pending.pluginDescription}
        </pre>
      )}
      <div className="mt-2 space-y-1 text-xs text-[var(--midnight)]">
        <MetaRow label="Severity" value={pending.pluginSeverity} />
        <MetaRow label="Plugin" value={pending.pluginId} />
        <MetaRow label="Agent" value={pending.agentId} />
        <MetaRow label="Session" value={pending.sessionKey} />
      </div>
    </>
  );
}

export function ExecApprovalCards() {
  const { queue, resolve } = useExecApprovalsContext();
  if (queue.length === 0) return null;
  return (
    <div className="px-4 pt-2 pb-1 flex flex-col gap-2 max-h-[40vh] overflow-y-auto">
      {queue.map((req) => (
        <ExecApprovalCard key={req.id} pending={req} onDecide={(decision) => resolve(req.id, decision)} />
      ))}
    </div>
  );
}

interface ExecApprovalCardProps {
  pending: ExecApprovalRequest;
  onDecide: (decision: ExecApprovalDecision) => Promise<void>;
}

function ExecApprovalCard({ pending, onDecide }: ExecApprovalCardProps) {
  const [pendingDecision, setPendingDecision] = useState<ExecApprovalDecision | null>(null);
  const [lastFailed, setLastFailed] = useState<ExecApprovalDecision | null>(null);

  const allowsOnce = pending.allowedDecisions.includes("allow-once");
  const allowsAlways = pending.allowedDecisions.includes("allow-always");
  const allowsDeny = pending.allowedDecisions.includes("deny");

  const trustScopeLine = pending.resolvedPath
    ? `Trust will always allow ${pending.resolvedPath} on this ${HOST_LABEL[pending.host]} (any arguments).`
    : `Trust will always allow this command on this ${HOST_LABEL[pending.host]} (any arguments).`;

  const submit = async (decision: ExecApprovalDecision) => {
    setPendingDecision(decision);
    try {
      await onDecide(decision);
      setLastFailed(null);
    } catch (e) {
      notifyApiError(e, "Couldn't send decision");
      setLastFailed(decision);
    } finally {
      setPendingDecision(null);
    }
  };

  return (
    <div className="rounded-md border border-[var(--edge)] bg-[var(--cream)] p-3 text-sm">
      {pending.kind === "plugin" ? (
        <>
          <div className="flex items-start justify-between gap-2">
            <div className="font-semibold text-[var(--midnight)] break-all">
              {pending.pluginTitle}
            </div>
          </div>
          <PluginBody pending={pending} />
        </>
      ) : (
        <>
          <div className="flex items-start justify-between gap-2">
            <CommandWithSpans command={pending.command} spans={pending.commandSpans} />
            <span className="inline-flex items-center px-2 py-0.5 text-xs rounded bg-cream-deep text-[var(--midnight)] shrink-0">
              {HOST_LABEL[pending.host]}
            </span>
          </div>

          <div className="mt-2 space-y-1 text-xs text-[var(--midnight)]">
            <MetaRow label="Host" value={HOST_LABEL[pending.host]} />
            <MetaRow label="Agent" value={pending.agentId} />
            <MetaRow label="Session" value={pending.sessionKey} />
            <MetaRow label="CWD" value={pending.cwd} path />
            <MetaRow label="Resolved" value={pending.resolvedPath} path />
            <MetaRow label="Security" value={pending.security} />
            <MetaRow label="Ask" value={pending.ask} />
            {pending.commandArgv && (
              <MetaRow label="argv" value={JSON.stringify(pending.commandArgv)} />
            )}
            {allowsAlways && (
              <div className="text-[var(--midnight-muted)] pt-1">{trustScopeLine}</div>
            )}
          </div>
        </>
      )}

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

      {lastFailed && (
        <div className="mt-2 text-xs text-err flex items-center gap-2">
          <span>That decision didn&apos;t send.</span>
          <button
            type="button"
            className="underline"
            onClick={() => submit(lastFailed)}
            disabled={pendingDecision !== null}
          >
            Retry
          </button>
        </div>
      )}
    </div>
  );
}
