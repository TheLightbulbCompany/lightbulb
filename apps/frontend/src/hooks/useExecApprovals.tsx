// frontend/src/hooks/useExecApprovals.ts
/**
 * Global exec-approval queue.
 *
 * Mirrors OpenClaw's native pattern (see upstream ui/src/ui/app-gateway.ts
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
import { isSessionKeyForUser } from "@/lib/sessionKey";

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
  /** Discriminates exec (shell command) vs plugin (MCP/plugin tool) approvals.
   *  Required so the union is exhaustive — both construction paths set it,
   *  and the card branches on it. */
  kind: "exec" | "plugin";
  /** Raw command line as the agent would execute it. For plugin approvals this
   *  slot mirrors `pluginTitle` so the queue stays homogeneous. */
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
  /** Byte-offset ranges into `command` marking the executable tokens
   *  (top-level + nested). Used to highlight what will actually run
   *  inside a compound/piped command. Validated against command length. */
  commandSpans?: { startIndex: number; endIndex: number }[];
  /** Effective exec security mode: deny | allowlist | full. */
  security?: string;
  /** Effective ask policy: off | on-miss | always. */
  ask?: string;
  /** Plugin approval title (also mirrored into `command` for the shared slot). */
  pluginTitle?: string;
  /** Plugin approval description (free text). */
  pluginDescription?: string | null;
  /** Plugin severity classification. */
  pluginSeverity?: string | null;
  /** Server-prefixed plugin id (e.g. "plugin:slack"). */
  pluginId?: string | null;
  /** Server-side expiry timestamp in ms. Used both for the live countdown
   *  and the auto-prune timer. */
  expiresAtMs: number;
  /** Server-side creation timestamp in ms, when the event supplies one.
   *  Paired with expiresAtMs (SAME server clock) it yields a skew-free
   *  intended lifetime for the auto-prune timer — see autoPruneDelayMs. */
  createdAtMs?: number;
  /** When this approval was added to the local queue (browser clock). */
  enqueuedAtMs: number;
}

export interface UseExecApprovalsReturn {
  /** FIFO queue of pending approvals. The dialog shows queue[0]. */
  queue: ExecApprovalRequest[];
  /** Submit a decision for an approval id. Removes it from the queue on success. */
  resolve: (id: string, decision: ExecApprovalDecision) => Promise<void>;
  /** Locally drop an approval from the queue without contacting the server.
   *
   *  Intentionally NOT wired to a UI button: a user-facing Dismiss on a
   *  still-valid approval creates a race — the server-side approval keeps
   *  the agent paused, but useAgentChat treats `dismissed` as "no
   *  continuation coming" and releases the busy gate, so a follow-up can
   *  race with the eventual resume. Reserved for internal callers that
   *  have already confirmed the approval is dead server-side (e.g. the
   *  auto-prune timer at expiresAtMs). User-driven "I want this gone"
   *  should go through `resolve(id, "deny")` so the server is informed. */
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
    commandSpans?: { startIndex: number; endIndex: number }[];
    security?: string;
    ask?: string;
  };
  createdAtMs?: number;
  expiresAtMs?: number;
}

interface PluginRequestPayload {
  id?: string;
  request?: {
    pluginId?: string;
    title?: string;
    description?: string;
    severity?: string;
    agentId?: string;
    sessionKey?: string;
    allowedDecisions?: ExecApprovalDecision[];
  };
  createdAtMs?: number;
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

/** Grace added to the auto-prune delay so an approval sitting right at its
 *  expiry boundary still renders (and stays actionable) briefly rather than
 *  vanishing on the same tick it arrives. */
const PRUNE_GRACE_MS = 500;

/** Tolerance for browser↔container clock skew, added to the browser's view of
 *  remaining time so a browser clock running AHEAD of the container can't prune
 *  a still-valid approval early (the T-2 bug). Also the crossover: for
 *  approvals whose remaining lifetime exceeds this, the browser's transit-aware
 *  estimate governs (delayed/replayed cards prune near their true remaining);
 *  for shorter ones we fall back to the full lifetime (fail-safe — a vanished
 *  live card is worse than a dead card lingering, since auto_pruned releases
 *  the approval-pause busy gate). */
const SKEW_TOLERANCE_MS = 2 * 60 * 1000;

/** Fallback horizon for a MALFORMED event that carries no server createdAtMs
 *  (real exec + plugin events both carry it — see the OpenClaw
 *  ExecApprovalRequest wire type). Without it we can't measure the lifetime, so
 *  we keep the card at least this long rather than risk pruning a live one.
 *  Mirrors upstream DEFAULT_EXEC_APPROVAL_TIMEOUT_MS (30 min). */
const KNOWN_APPROVAL_LIFETIME_MS = 30 * 60 * 1000;

/** Compute the local auto-prune delay for an approval.
 *
 *  The bug this avoids (T-2): the old `expiresAtMs - Date.now()` subtracts a
 *  BROWSER clock (Date.now) from a CONTAINER clock (expiresAtMs); a browser
 *  running ahead shrinks — or zeroes — the delay and auto-prunes a live
 *  approval (which also releases the approval-pause busy gate, letting a
 *  follow-up race the still-waiting run).
 *
 *  Both exec and plugin events carry the container's `createdAtMs` +
 *  `expiresAtMs`. We take the browser's transit/replay-aware view of remaining
 *  time (`expiresAtMs - Date.now()`) — so a late-delivered or already-expired
 *  card prunes near its TRUE remaining, not a full lifetime restarted from
 *  receipt — then:
 *    - add SKEW_TOLERANCE_MS, so a browser ahead of the container by up to that
 *      much never prunes a live card early;
 *    - cap at the server-clock full lifetime (`expiresAtMs - createdAtMs`,
 *      skew-free), so a browser running BEHIND can't over-keep the card.
 *
 *  Residuals (no server-send timestamp is on the payload, so these are
 *  irreducible here — the proper fix is an upstream `sentAtMs` on the event):
 *    - a browser ahead by MORE than SKEW_TOLERANCE_MS prunes early by the
 *      excess (bounded; the card reappears on the next request and the server
 *      still holds the approval);
 *    - for approvals whose lifetime is shorter than SKEW_TOLERANCE_MS, a
 *      delayed/replayed delivery can't be told apart from clock skew, so the
 *      card is kept up to its full lifetime (clear replays — expiry past by
 *      more than the tolerance — still prune promptly). */
function autoPruneDelayMs(req: ExecApprovalRequest): number {
  const browserRemaining = req.expiresAtMs - Date.now();
  if (
    typeof req.createdAtMs === "number" &&
    Number.isFinite(req.createdAtMs) &&
    req.expiresAtMs > req.createdAtMs
  ) {
    const fullLifetime = req.expiresAtMs - req.createdAtMs;
    const delay = Math.min(browserRemaining + SKEW_TOLERANCE_MS, fullLifetime);
    return Math.max(0, delay) + PRUNE_GRACE_MS;
  }
  // Malformed event (no server createdAtMs): keep at least the known default
  // approval lifetime (fail-safe) rather than risk pruning a live approval.
  return (
    Math.max(KNOWN_APPROVAL_LIFETIME_MS, browserRemaining + SKEW_TOLERANCE_MS) +
    PRUNE_GRACE_MS
  );
}

/** Validate command spans against the command. Ported from upstream
 *  parseCommandSpans — drops out-of-range / malformed spans, returns
 *  undefined when nothing valid remains so the renderer falls back to
 *  plain text. WHY: a span that overruns command.length would slice into
 *  nothing and produce an empty <mark>. */
function parseCommandSpans(
  value: unknown,
  commandLength: number,
): { startIndex: number; endIndex: number }[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const spans = value.filter(
    (item): item is { startIndex: number; endIndex: number } => {
      if (!item || typeof item !== "object") return false;
      const { startIndex, endIndex } = item as Record<string, unknown>;
      return (
        typeof startIndex === "number" &&
        typeof endIndex === "number" &&
        Number.isSafeInteger(startIndex) &&
        Number.isSafeInteger(endIndex) &&
        startIndex >= 0 &&
        endIndex > startIndex &&
        endIndex <= commandLength
      );
    },
  );
  return spans.length > 0 ? spans : undefined;
}

function parseRequest(payload: unknown): ExecApprovalRequest | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as RequestPayload;
  if (!p.id || !p.request?.command) return null;
  const nowMs = Date.now();
  // Only synthesize an expiry when the server omitted (or sent a malformed)
  // timestamp. A valid server expiresAtMs (past or future) is preserved and
  // fed to autoPruneDelayMs, which derives the auto-prune delay from a
  // server-clock lifetime when createdAtMs is present (skew-free) and a
  // floored fallback otherwise — never a raw Date.now cross-clock subtraction.
  const hasValidServerTimestamp =
    typeof p.expiresAtMs === "number" && Number.isFinite(p.expiresAtMs);
  const expiresAtMs = hasValidServerTimestamp
    ? p.expiresAtMs!
    : nowMs + DEFAULT_EXPIRY_MS;
  const createdAtMs =
    typeof p.createdAtMs === "number" && Number.isFinite(p.createdAtMs)
      ? p.createdAtMs
      : undefined;
  return {
    id: p.id,
    kind: "exec",
    command: p.request.command,
    commandArgv: p.request.commandArgv,
    host: p.request.host ?? "gateway",
    cwd: p.request.cwd,
    resolvedPath: p.request.resolvedPath,
    agentId: p.request.agentId,
    sessionKey: p.request.sessionKey,
    allowedDecisions: p.request.allowedDecisions ?? ["allow-once", "deny"],
    commandSpans: parseCommandSpans(
      p.request.commandSpans,
      p.request.command.length,
    ),
    security: p.request.security,
    ask: p.request.ask,
    expiresAtMs,
    createdAtMs,
    enqueuedAtMs: nowMs,
  };
}

/** Parse a plugin.approval.requested payload into the shared queue shape.
 *  Reuses `command` as the title slot so the queue stays homogeneous;
 *  the card branches on `kind` for the body. Ported from upstream
 *  parsePluginApprovalRequested. */
function parsePluginRequest(payload: unknown): ExecApprovalRequest | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as PluginRequestPayload;
  const title = p.request?.title;
  if (!p.id || !title) return null;
  const nowMs = Date.now();
  // Mirror parseRequest's lenient expiry handling: only synthesize when the
  // server omitted (or sent a malformed) timestamp. Plugin events carry a
  // server createdAtMs, so autoPruneDelayMs gets a skew-free server-clock
  // lifetime (expiresAtMs - createdAtMs) for the auto-prune timer.
  const hasValidServerTimestamp =
    typeof p.expiresAtMs === "number" && Number.isFinite(p.expiresAtMs);
  const expiresAtMs = hasValidServerTimestamp
    ? p.expiresAtMs!
    : nowMs + DEFAULT_EXPIRY_MS;
  const createdAtMs =
    typeof p.createdAtMs === "number" && Number.isFinite(p.createdAtMs)
      ? p.createdAtMs
      : undefined;
  return {
    id: p.id,
    kind: "plugin",
    command: title, // shared slot; card heading uses pluginTitle
    host: "gateway", // not meaningful for plugins; satisfies the union
    agentId: p.request?.agentId,
    sessionKey: p.request?.sessionKey,
    // The plugin-approval contract defaults a missing allowedDecisions to the
    // FULL set including allow-always (Trust): upstream
    // resolvePluginApprovalRequestAllowedDecisions falls back to
    // DEFAULT_PLUGIN_APPROVAL_DECISIONS, and the server validates a resolve
    // against that default — so it would accept allow-always. Dropping it here
    // would disable the Trust button and force re-approval of the same plugin
    // action every time. (Exec's default deliberately omits Trust — see
    // parseRequest above — because exec only offers it in certain security modes.)
    allowedDecisions: p.request?.allowedDecisions ?? [
      "allow-once",
      "allow-always",
      "deny",
    ],
    expiresAtMs,
    createdAtMs,
    enqueuedAtMs: nowMs,
    pluginTitle: title,
    pluginDescription: p.request?.description ?? null,
    pluginSeverity: p.request?.severity ?? null,
    pluginId: p.request?.pluginId ?? null,
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
 * Delegates to the shared session-key parser so this and the structured-
 * question queue read the wire format the same way. That parser takes the
 * member id from the THIRD segment; the previous trailing-segment reading
 * here silently dropped every approval raised inside a NAMED webchat
 * session (`agent:<id>:<user>:<uuid>`), whose last segment is a uuid.
 */
function isApprovalForCurrentUser(
  req: ExecApprovalRequest,
  userId: string | null | undefined,
): boolean {
  return isSessionKeyForUser(req.sessionKey, userId);
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

  // Mirror of `queue` so resolve() can read an entry's kind synchronously
  // without widening its (id, decision) signature — the RPC method differs
  // between exec and plugin approvals.
  const queueRef = useRef<ExecApprovalRequest[]>([]);
  useEffect(() => {
    queueRef.current = queue;
  }, [queue]);

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
      // ui/src/ui/app-gateway.ts — without this, a never-resolved
      // approval sits in the queue indefinitely. Delay is derived from a
      // server-clock lifetime (skew-free) — see autoPruneDelayMs.
      const timer = setTimeout(
        () => removeFromQueue(req.id, "auto_pruned"),
        autoPruneDelayMs(req),
      );
      timersRef.current.set(req.id, timer);
    });
    return unsub;
  }, [onEvent, removeFromQueue]);

  // Subscribe to plugin.approval.requested — enqueue plugin approvals into
  // the same queue. Same multi-tenant guard + dedup + auto-prune as exec.
  useEffect(() => {
    const unsub = onEvent((eventName, data) => {
      if (eventName !== "plugin.approval.requested") return;
      const req = parsePluginRequest(data);
      if (!req) return;
      if (!isApprovalForCurrentUser(req, userIdRef.current)) return;
      if (trackedIdsRef.current.has(req.id)) return;
      trackedIdsRef.current.add(req.id);
      setQueue((prev) => [...prev, req]);
      const timer = setTimeout(
        () => removeFromQueue(req.id, "auto_pruned"),
        autoPruneDelayMs(req),
      );
      timersRef.current.set(req.id, timer);
    });
    return unsub;
  }, [onEvent, removeFromQueue]);

  // Subscribe to {exec,plugin}.approval.resolved — drop server-resolved
  // approvals. This fires when the approval was decided through some other
  // channel (CLI, another tab, the desktop bridge), not just our own
  // resolve(). Both payloads are shaped `{id}`.
  useEffect(() => {
    const unsub = onEvent((eventName, data) => {
      if (
        eventName !== "exec.approval.resolved" &&
        eventName !== "plugin.approval.resolved"
      )
        return;
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
      // Route to the matching RPC by the entry's kind — plugin approvals
      // resolve through plugin.approval.resolve, exec through exec's. Read
      // the kind from the queue mirror so the (id, decision) signature stays
      // narrow. Default to exec when the entry is unknown (already pruned).
      const entry = queueRef.current.find((e) => e.id === id);
      const method =
        entry?.kind === "plugin"
          ? "plugin.approval.resolve"
          : "exec.approval.resolve";
      try {
        await sendReq(method, { id, decision });
        // Optimistically remove. The server will also emit
        // {exec,plugin}.approval.resolved which is a no-op since we already
        // removed.
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
