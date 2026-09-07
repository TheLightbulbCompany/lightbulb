/**
 * Wire types for the Isol8 gateway WebSocket protocol — the frames the
 * backend's connection pool forwards from a user's OpenClaw container.
 * Extracted from the web frontend's useGateway.tsx (Phase 1); both clients
 * must speak exactly this protocol, so changes here are protocol changes.
 */

/**
 * OpenClaw tool result content blocks — text blocks or image refs. Image
 * data is replaced with `{ bytes, omitted: true }` by OpenClaw's sanitizer.
 */
export type ToolResultBlock = { type: string; text?: string; bytes?: number; omitted?: boolean };

export type ChatIncomingMessage =
  // Streaming text. `append: true` means `content` is an incremental delta to
  // append to the run's accumulated text; absent means `content` is the full
  // text of the CURRENT assistant message only. Upstream resets its
  // accumulator at every assistant message_start (i.e. after every tool
  // call), so mid-run non-append content is SEGMENT-scoped, not
  // run-cumulative. Sent as the first frame per (connection, run), for
  // OpenClaw `replace: true` events, and as the chat.final backstop. A
  // caller tracking a committed segment offset must rebuild non-append
  // frames as frozen-prefix + content (see the mobile agent screen's chunk
  // handler) rather than folding them through accumulateStreamText(),
  // whose replace branch is only run-correct before the first tool call —
  // see ./streamText.
  | { type: "chunk"; content: string; append?: boolean; agent_id?: string; runId: string; session_id?: string }
  | { type: "thinking"; content: string; append?: boolean; agent_id?: string; runId: string; session_id?: string }
  | { type: "done"; agent_id?: string; runId: string; session_id?: string }
  | { type: "error"; message: string; code?: string; agent_id?: string; runId?: string; session_id?: string }
  // Authoritative "run truly ended" frame. ADDITIONAL to done/error/aborted —
  // never a replacement. The backend forwards it EXACTLY ONCE per run on a
  // genuine termination (NOT on the interim chat.final that closes a
  // tool-approval bracket). The chat queue drains off this signal, not a
  // local terminal heuristic. See backend connection_pool.py run_ended forward.
  | { type: "run_ended"; runId: string; outcome: "done" | "error" | "aborted"; agent_id?: string; session_id?: string }
  // Server-truth busy signal. The backend subscribes to OpenClaw's
  // sessions.changed and forwards this whenever a run starts/stops on the
  // container, independent of any particular tab's local isStreaming
  // anchor — see useAgentChat's `busy = isStreaming || containerRunActive`.
  | { type: "run_active"; active: boolean; agent_id?: string; session_id?: string }
  | { type: "heartbeat" }
  | {
      type: "tool_start";
      tool: string;
      toolCallId?: string;
      args?: Record<string, unknown>;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  | {
      type: "tool_end";
      tool: string;
      toolCallId?: string;
      result?: ToolResultBlock[];
      meta?: string;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  | {
      type: "tool_error";
      tool: string;
      toolCallId?: string;
      result?: ToolResultBlock[];
      meta?: string;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  // Live incremental tool output (OpenClaw tool stream phase:"update").
  // Attached to the running tool pill addressed by toolCallId.
  | {
      type: "tool_progress";
      tool: string;
      toolCallId: string;
      partialResult?: ToolResultBlock[];
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  // Live command stdout/stderr streamed mid-exec, appended to the tool pill
  // addressed by toolCallId.
  | {
      type: "command_output";
      toolCallId: string;
      output?: string;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  // Low-noise agent-activity surfaces (proposed plan, patch summary, item
  // lifecycle, ACP step) folded into one pill. `detail` carries the full
  // upstream payload for the pill's expansion.
  //
  // `run_status` and `compaction` ride the same envelope but are NOT pills:
  // they describe the run's current state (startup phase, history compaction)
  // rather than an event, so the chat hook routes them to the run-status strip
  // and returns before the pill logic. `detail.phase` is the payload that
  // matters on both; compaction also carries `completed`/`willRetry`.
  | {
      type: "agent_activity";
      activity: "plan" | "patch" | "item" | "acp" | "run_status" | "compaction";
      title: string;
      detail?: Record<string, unknown>;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  // A `/btw` (detached side-channel) answer. OpenClaw renders these as a
  // separate side card ("Not saved to chat history"); the backend maps
  // chat.side_result → this frame. Ephemeral — never folded into the
  // transcript/history. runId is the side run's id (optional).
  | { type: "btw_result"; question: string; text: string; isError?: boolean; agent_id?: string; runId?: string; session_id?: string }
  | { type: "update_available" };

/** Gateway event forwarded from OpenClaw */
export interface GatewayEvent {
  type: "event";
  event: string;
  payload: unknown;
}

// =============================================================================
// Structured questions (OpenClaw 2026.8 `ask_user` / `secrets` tools)
// =============================================================================
//
// The container's `ask_user` tool BLOCKS the run in-tool (default 900s) until
// an operator answers, so these records are the only thing that unblocks it.
// The gateway broadcasts `question.requested` / `question.resolved`, which the
// backend forwards verbatim as `{type:"event", event, payload}` — see
// connection_pool.py's generic event branch. Mirrors upstream
// packages/gateway-protocol/src/schema/questions.ts; keep them in step.

/** One selectable choice. `options` is either empty (free text) or 2–4 long. */
export interface QuestionOption {
  label: string;
  description?: string;
}

/** Where a secret answer is stored, and the consent policy shown for it. */
export interface QuestionSecretStoreBinding {
  /** Store entry name, `^[A-Z][A-Z0-9_]{0,127}$` (e.g. STRIPE_API_KEY). */
  name: string;
  /** Only "secret" is accepted for masked prompts; "env" is Settings/CLI-only. */
  kind: "secret" | "env";
  /** Hostnames the stored value may be sent to. Server-normalized. */
  allowedHosts?: string[];
  reason?: string;
}

export interface Question {
  /** `^[a-z][a-z0-9_]*$`; the key answers are posted under. */
  questionId: string;
  /** Short chip label, ≤12 chars. */
  header: string;
  question: string;
  options: QuestionOption[];
  multiSelect?: boolean;
  /** Allow a free-text answer alongside the options. */
  isOther?: boolean;
  /** Render masked. Always paired with `secretStore` — the gateway rejects a
   *  secret question without a binding. */
  isSecret?: boolean;
  secretStore?: QuestionSecretStoreBinding;
  /** Present when the store already holds an entry this answer would overwrite. */
  secretStoreExisting?: { updatedAtMs: number; updatedBy?: string };
}

/** Note the double nesting — `question.resolve` takes `{answers: {answers}}`. */
export interface QuestionAnswers {
  answers: Record<string, string[]>;
}

export type QuestionStatus = "pending" | "answered" | "cancelled" | "expired";

/** One pending or recently resolved question request. Payload of the
 *  `question.requested` event and of each `question.list` entry. */
export interface QuestionRecord {
  id: string;
  /** 1–3 questions, answered together in one resolve. */
  questions: Question[];
  agentId?: string;
  sessionKey?: string;
  runId?: string;
  createdAtMs: number;
  expiresAtMs: number;
  status: QuestionStatus;
  answers?: QuestionAnswers;
  resolvedBy?: string;
}

/** Payload of the `question.resolved` event. */
export type QuestionResolvedEvent =
  | { id: string; status: "answered"; answers: QuestionAnswers }
  | { id: string; status: "cancelled" }
  | { id: string; status: "expired" };
