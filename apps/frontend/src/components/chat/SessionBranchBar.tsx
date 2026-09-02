// frontend/src/components/chat/SessionBranchBar.tsx
/**
 * Transcript branch switcher (OpenClaw 8.1 `sessions.branches.*`).
 *
 * Rewinding a session does not delete the tail — it repoints the active path
 * and leaves the old tail as a tip in the transcript DAG. `sessions.branches
 * .list` returns every tip; `sessions.branches.switch` (via `onSwitch`, which
 * useAgentChat owns because it also has to refetch history) walks back to one.
 *
 * A session with a single tip has nothing to choose between, which is every
 * session that has never been rewound — so the bar renders NOTHING until a
 * second branch exists rather than occupying the transcript with a control
 * that does nothing.
 */
"use client";

import { useEffect } from "react";
import { cn } from "@/lib/utils";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";

/** One transcript DAG tip. Mirrors the fork's SessionBranchSchema. */
export interface SessionBranch {
  leafEntryId: string;
  headline: string;
  messageCount: number;
  updatedAt?: string;
  active: boolean;
}

export function SessionBranchBar({
  sessionKey,
  onSwitch,
  branchRefreshToken,
  disabled,
}: {
  sessionKey: string | null;
  /** Repoint the transcript to this tip. Resolves false if the RPC failed. */
  onSwitch: (leafEntryId: string) => Promise<boolean>;
  /** Advances whenever a rewind/switch reshaped the DAG — re-read on the edge. */
  branchRefreshToken: number;
  /** A run is live; the container refuses a branch switch mid-run. */
  disabled?: boolean;
}) {
  const { data, mutate } = useGatewayRpc<{ branches?: SessionBranch[] }>(
    sessionKey ? "sessions.branches.list" : null,
    sessionKey ? { sessionKey } : undefined,
    // A container that doesn't answer this method must not be re-asked 100
    // times at 3s (useGatewayRpc's cold-start ladder). The branch list is
    // decoration; one attempt plus the refresh token below is enough.
    { shouldRetryOnError: false },
  );

  useEffect(() => {
    if (branchRefreshToken > 0) void mutate();
  }, [branchRefreshToken, mutate]);

  const branches = data?.branches ?? [];
  if (branches.length <= 1) return null;

  return (
    <div className="shrink-0 flex items-center gap-1.5 overflow-x-auto border-b border-[var(--edge)] bg-[var(--cream-deep)] px-4 py-1.5">
      <span className="label-caps shrink-0 text-[var(--midnight-muted)]">Branches</span>
      {branches.map((b) => (
        <button
          key={b.leafEntryId}
          type="button"
          aria-current={b.active ? "true" : undefined}
          disabled={b.active || disabled}
          onClick={() => void onSwitch(b.leafEntryId)}
          className={cn(
            "shrink-0 rounded-full border px-2.5 py-0.5 text-caption transition-colors",
            b.active
              ? "border-[var(--midnight)] text-[var(--midnight)]"
              : "border-[var(--edge)] text-[var(--midnight-muted)] hover:border-[var(--midnight-muted)] hover:text-[var(--midnight)]",
            "disabled:cursor-default",
          )}
        >
          {b.headline || "Untitled"} · {b.messageCount} messages
        </button>
      ))}
    </div>
  );
}
