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
  | { type: "chunk"; content: string; agent_id?: string; runId: string; session_id?: string }
  | { type: "thinking"; content: string; agent_id?: string; runId: string; session_id?: string }
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
  | {
      type: "agent_activity";
      activity: "plan" | "patch" | "item" | "acp";
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
