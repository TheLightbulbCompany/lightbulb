// frontend/src/hooks/useExecApprovals.ts
/**
 * Global exec-approval queue.
 *
 * Mirrors OpenClaw's native pattern (see ~/Desktop/openclaw/ui/src/ui/app-gateway.ts
 * `enqueueApprovalRequest` and ui/src/ui/views/exec-approval.ts):
 *
 *   - Approvals live in a separate queue, NOT bound to chat message bubbles.
 *   - The dialog renders the head of the queue at the app shell level so
 *     it's always visible regardless of which chat tab the user is on.
 *   - Each request auto-prunes at expiresAtMs — no orphan accumulation.
 *
 * The previous implementation glued each pending approval to a tool-use
 * bubble inside `messages` state. When the binding fell back to "newest
 * running exec" and that exec was in an old/stuck bubble, the approval
 * card landed offscreen and the user couldn't act on it. Approvals then
 * accumulated in `messages` forever (no expiry handling), and Ray fell
 * back to telling users to type `/approve <id>` as a chat command.
 *
 * This hook subscribes to the gateway's `exec.approval.requested` and
 * `exec.approval.resolved` events at the global level and exposes a
 * stable API for the modal dialog. The queue is per-user (one gateway
 * connection = one queue) and shows approvals from any agent that user
 * has — the operator approves on behalf of themselves regardless of
 * which agent tab is active.
 */

"use client";

import * as React from "react";
import { useEffect, useState, useCallback, useRef } from "react";
import { useAuth } from "@clerk/nextjs";
import { useGateway } from "@/hooks/useGateway";

export type ExecApprovalDecision = "allow-once" | "allow-always" | "deny";

/** Why a particular approval was removed from the queue. Consumers care
 *  about this distinction because the server-side run continues only
 *  after `resolved` — for the other reasons there's no continuation, so
 *  any busy-gate held across the pending approval can release. */
export type ApprovalRemovalReason =
  | "resolved"        // server accepted resolve OR exec.approval.resolved event
  | "dismissed"       // user clicked Dismiss locally
  | "auto_pruned"     // local expiry timer fired
  | "terminal_error"; // server rejected with unknown/expired id

export type ApprovalRemovedHandler = (
  id: string,
  reason: ApprovalRemovalReason,
) => void;

export interface ExecApprovalRequest {
  /** Approval ID issued by OpenClaw. Used as the key when posting exec.approval.resolve. */
  id: string;
  /** Raw command line as the agent would execute it. */
  command: string;
  /** Parsed argv; absent when the request came through host=node with a wrapped form. */
  commandArgv?: string[];
  /** Where the command would run. */
  host: "gateway" | "node" | "sandbox";
  /** Working directory for the command. */
  cwd?: string;
  /** Resolved absolute path of the executable (post wrapper-unwrap) — what Trust persists. */
  resolvedPath?: string;
  /** OpenClaw agent ID that issued the exec. */
  agentId?: string;
  /** Session identifier: used for audit display only. */
  sessionKey?: string;
  /** Which decisions the server will accept — usually all three, but
   *  "allow-always" may be absent when policy is ask=always. */
  allowedDecisions: ExecApprovalDecision[];
  /** Server-side expiry timestamp in ms. Used both for the live countdown
   *  and the auto-prune timer. */
  expiresAtMs: number;
  /** When this approval was added to the local queue. */
  enqueuedAtMs: number;
}

export interface UseExecApprovalsReturn {
  /** FIFO queue of pending approvals. The dialog shows queue[0]. */
  queue: ExecApprovalRequest[];
  /** Submit a decision for an approval id. Removes it from the queue on success. */
  resolve: (id: string, decision: ExecApprovalDecision) => Promise<void>;
  /** Locally drop an approval from the queue without contacting the server.
   *  Used as an escape hatch when the server has lost the id (container
   *  restart, post-expiry retry) and the user just wants the card gone. */
  dismiss: (id: string) => void;
  /** Subscribe to removal events with a reason. Returns an unsubscribe fn.
   *  useAgentChat uses this to release the approval-pause busy gate only
   *  on dismiss/auto_pruned/terminal_error — `resolved` removals must keep
   *  the gate armed so the post-approval continuation isn't raced. */
  onApprovalRemoved: (handler: ApprovalRemovedHandler) => () => void;
  /** Last error encountered while resolving (cleared on the next resolve attempt). */
  error: string | null;
}

interface RequestPayload {
  id?: string;
  request?: {
    command?: string;
    commandArgv?: string[];
    host?: ExecApprovalRequest["host"];
    cwd?: string;
    resolvedPath?: string;
    agentId?: string;
    sessionKey?: string;
    allowedDecisions?: ExecApprovalDecision[];
  };
  expiresAtMs?: number;
}

interface ResolvedPayload {
  id?: string;
  decision?: ExecApprovalDecision;
}

/** Default approval lifetime when the server doesn't supply expiresAtMs.
 *  OpenClaw's gateway emits this field, but we set a generous local fallback
 *  (10 min) so a malformed event still gets pruned eventually. */
const DEFAULT_EXPIRY_MS = 10 * 60 * 1000;

function parseRequest(payload: unknown): ExecApprovalRequest | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as RequestPayload;
  if (!p.id || !p.request?.command) return null;
  const nowMs = Date.now();
  // Only synthesize an expiry when the server omitted (or sent a malformed)
  // timestamp. Server-supplied past timestamps must be preserved so that
  // delayed/replayed events get pruned immediately by the local timer
  // (Math.max(0, expiresAtMs - now + 500) → 500ms when in the past) rather
  // than getting extended for another DEFAULT_EXPIRY_MS at the head of the
  // queue with live buttons.
  const hasValidServerTimestamp =
    typeof p.expiresAtMs === "number" && Number.isFinite(p.expiresAtMs);
  const expiresAtMs = hasValidServerTimestamp
    ? p.expiresAtMs!
    : nowMs + DEFAULT_EXPIRY_MS;
  return {
    id: p.id,
    command: p.request.command,
    commandArgv: p.request.commandArgv,
    host: p.request.host ?? "gateway",
    cwd: p.request.cwd,
    resolvedPath: p.request.resolvedPath,
    agentId: p.request.agentId,
    sessionKey: p.request.sessionKey,
    allowedDecisions: p.request.allowedDecisions ?? ["allow-once", "deny"],
    expiresAtMs,
    enqueuedAtMs: nowMs,
  };
}

/**
 * Returns true when the request is for the currently signed-in member.
 *
 * In an org with multiple connected members, the backend's
 * connection_pool.py routes events by top-level `payload.sessionKey`,
 * but exec.approval.requested carries `sessionKey` under `payload.request`
 * — so the routing falls through to the broadcast path, and every
 * member's browser receives every member's approval. Without this
 * filter, member B could see and resolve member A's pending exec.
 *
 * sessionKey format from OpenClaw: `agent:<agentId>:<memberId>` where
 * `<memberId>` is the Clerk user.id lowercased. We isolate the trailing
 * member-id segment and compare it case-insensitively for an exact
 * match. A naive `includes` would incorrectly accept a longer member
 * id whose suffix matches the current user's (e.g. user `user_abc` would
 * match `agent:.../user_abcd`).
 *
 * If the request has no sessionKey (defensive: malformed event, or
 * legacy emitter), we still accept — the alternative is dropping
 * legitimate events on personal accounts that may not always populate
 * sessionKey. The broadcast leak this guards against only manifests in
 * org contexts, where sessionKey is always present.
 */
function isApprovalForCurrentUser(
  req: ExecApprovalRequest,
  userId: string | null | undefined,
): boolean {
  if (!userId) return false; // not signed in — drop everything
  if (!req.sessionKey) return true; // no scoping signal; accept (legacy emitters)
  const segments = req.sessionKey.split(":");
  const memberId = segments[segments.length - 1];
  return memberId.toLowerCase() === userId.toLowerCase();
}

export function useExecApprovals(): UseExecApprovalsReturn {
  const { onEvent, sendReq } = useGateway();
  const { userId } = useAuth();
  const userIdRef = useRef(userId);
  useEffect(() => {
    userIdRef.current = userId;
  }, [userId]);
  const [queue, setQueue] = useState<ExecApprovalRequest[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Per-id auto-prune timers. Cleared when the entry is removed (resolved
  // by the user, server-resolved, or expired locally).
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // Subscribers notified on every removal, with the reason. useAgentChat
  // uses this to decide whether to release the approval-pause busy gate
  // (only on dismiss/auto_pruned/terminal_error — `resolved` keeps it
  // armed across the continuation window).
  const removalHandlersRef = useRef<Set<ApprovalRemovedHandler>>(new Set());

  // Mirror of currently-enqueued ids, kept in sync with `queue` for the
  // express purpose of giving removeFromQueue a synchronous "was this id
  // present?" check. Avoids depending on setQueue's updater (which can
  // run twice in strict mode) to detect actual removals.
  const trackedIdsRef = useRef<Set<string>>(new Set());

  const removeFromQueue = useCallback(
    (id: string, reason: ApprovalRemovalReason) => {
      const timer = timersRef.current.get(id);
      if (timer !== undefined) {
        clearTimeout(timer);
        timersRef.current.delete(id);
      }
      // Only notify on actual removal — re-entrant or duplicate calls
      // (e.g. server `exec.approval.resolved` after our own resolve()
      // already pruned) shouldn't double-fire. We dedupe via a separate
      // ref of currently-tracked ids, set when we enqueue and cleared
      // here, so the check is independent of React's batched setQueue
      // (which can run the updater twice in strict mode).
      const wasPresent = trackedIdsRef.current.delete(id);
      setQueue((prev) => prev.filter((entry) => entry.id !== id));
      if (wasPresent) {
        for (const h of removalHandlersRef.current) {
          try {
            h(id, reason);
          } catch {
            // subscriber error, ignore
          }
        }
      }
    },
    [],
  );

  const onApprovalRemoved = useCallback(
    (handler: ApprovalRemovedHandler) => {
      removalHandlersRef.current.add(handler);
      return () => {
        removalHandlersRef.current.delete(handler);
      };
    },
    [],
  );

  // Subscribe to exec.approval.requested — enqueue new approvals.
  useEffect(() => {
    const unsub = onEvent((eventName, data) => {
      if (eventName !== "exec.approval.requested") return;
      const req = parseRequest(data);
      if (!req) return;
      // Multi-tenancy guard: drop approvals targeting other org members.
      // See isApprovalForCurrentUser for the why and the routing context.
      if (!isApprovalForCurrentUser(req, userIdRef.current)) return;

      // Idempotent: a retry/reconnect can redeliver the same approval.
      if (trackedIdsRef.current.has(req.id)) return;
      trackedIdsRef.current.add(req.id);
      setQueue((prev) => [...prev, req]);

      // Schedule auto-prune at expiry. Matches OpenClaw's pattern at
      // ui/src/ui/app-gateway.ts:140 — without this, a never-resolved
      // approval sits in the queue indefinitely.
      const delay = Math.max(0, req.expiresAtMs - Date.now() + 500);
      const timer = setTimeout(
        () => removeFromQueue(req.id, "auto_pruned"),
        delay,
      );
      timersRef.current.set(req.id, timer);
    });
    return unsub;
  }, [onEvent, removeFromQueue]);

  // Subscribe to exec.approval.resolved — drop server-resolved approvals.
  // This fires when the approval was decided through some other channel
  // (CLI, another tab, the desktop bridge), not just our own resolve().
  useEffect(() => {
    const unsub = onEvent((eventName, data) => {
      if (eventName !== "exec.approval.resolved") return;
      const p = data as ResolvedPayload;
      if (!p?.id) return;
      removeFromQueue(p.id, "resolved");
    });
    return unsub;
  }, [onEvent, removeFromQueue]);

  // Cleanup all timers on unmount.
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, []);

  const resolve = useCallback(
    async (id: string, decision: ExecApprovalDecision): Promise<void> => {
      setError(null);
      try {
        await sendReq("exec.approval.resolve", { id, decision });
        // Optimistically remove. The server will also emit
        // exec.approval.resolved which is a no-op since we already removed.
        removeFromQueue(id, "resolved");
      } catch (e) {
        const msg = e instanceof Error ? e.message : "Failed to send decision";
        // OpenClaw's approval registry is in-memory in the per-user container.
        // After a container restart — or after the server-side TTL fires
        // before our local `expiresAtMs` timer (clock drift, late-delivered
        // event) — the id is no longer registered and the server returns
        // INVALID_REQUEST with "unknown or expired approval id". The
        // approval is dead; retrying it just produces the same error
        // forever. Drop it locally and return without surfacing the error
        // so the stale card disappears instead of wedging the queue.
        if (/unknown.*approval|expired.*approval/i.test(msg)) {
          removeFromQueue(id, "terminal_error");
          return;
        }
        setError(msg);
        throw e;
      }
    },
    [sendReq, removeFromQueue],
  );

  const dismiss = useCallback(
    (id: string) => {
      removeFromQueue(id, "dismissed");
    },
    [removeFromQueue],
  );

  return { queue, resolve, dismiss, onApprovalRemoved, error };
}

// =============================================================================
// React Context — single instance per app shell so the dialog and any future
// consumers (e.g. a header counter badge) share state without re-subscribing.
// =============================================================================

const ExecApprovalsContext = React.createContext<UseExecApprovalsReturn | null>(null);

export function ExecApprovalsProvider({ children }: { children: React.ReactNode }) {
  const value = useExecApprovals();
  return (
    <ExecApprovalsContext.Provider value={value}>{children}</ExecApprovalsContext.Provider>
  );
}

export function useExecApprovalsContext(): UseExecApprovalsReturn {
  const ctx = React.useContext(ExecApprovalsContext);
  if (!ctx) {
    throw new Error(
      "useExecApprovalsContext must be used inside <ExecApprovalsProvider>",
    );
  }
  return ctx;
}

/** Stable empty default for `useExecApprovalsContextOptional` — keeps
 *  referential identity across renders so dependent effects don't refire
 *  on every parent render when the provider is absent. */
const EMPTY_APPROVALS_CONTEXT: UseExecApprovalsReturn = {
  queue: [],
  resolve: async () => {},
  dismiss: () => {},
  onApprovalRemoved: () => () => {},
  error: null,
};

/** Like `useExecApprovalsContext` but returns an empty queue + no-op
 *  handlers when called outside the provider. For consumers like
 *  `useAgentChat` that depend on the approval queue but must still work
 *  in test contexts (or any future surface) where the provider isn't
 *  mounted. */
export function useExecApprovalsContextOptional(): UseExecApprovalsReturn {
  return React.useContext(ExecApprovalsContext) ?? EMPTY_APPROVALS_CONTEXT;
}
