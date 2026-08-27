/**
 * chat.history semantics — what a transcript message MEANS.
 *
 * This is the OpenClaw-facing half of history rendering: the wire shapes, the
 * visibility filter, and the block/tool-id helpers. It is deliberately free of
 * any display model, because the two clients legitimately have different ones
 * (web builds ChatRenderItem groups; mobile builds a flat ChatItem list). Each
 * app keeps its own projection on top of this.
 *
 * It lives here because it was previously implemented TWICE — web's
 * `hooks/internal/{types,chatHistoryFilters,toolBlockExtract}.ts` and mobile's
 * `lib/chatHistory.ts` — and the copies had silently drifted apart:
 *
 *   - `resolveToolUseId` accepted three id spellings on web and four on
 *     mobile, so a tool_result keyed by `toolCallId` paired on mobile and
 *     orphaned on web (its pill rendered with no result).
 *   - `isEmptyUserTextOnlyMessage` compared block types case-SENSITIVELY on
 *     web while every other type check on both clients lowercased.
 *
 * Both are resolved here toward the more permissive reading, which is the one
 * that renders more of the user's actual transcript. When re-syncing against
 * upstream, the source of truth is `ui/src/pages/chat/chat-history.ts` and
 * `src/chat/tool-content.ts` in the Lightbulb AI OpenClaw fork (`Isol8AI/agent`).
 */

// ---------------------------------------------------------------------------
// Wire shapes
// ---------------------------------------------------------------------------

/** A content block within a HistoryMessage. Field shapes vary by `type`. */
export interface ContentBlock {
  /** Optional: wire data does not guarantee it. Read via the block predicates. */
  type?: string;
  // text-flavored
  text?: string;
  // thinking-flavored
  thinking?: string;
  // tool_use-flavored
  id?: string;
  name?: string;
  args?: unknown;
  arguments?: unknown;
  input?: unknown;
  /** Live incremental output (tool phase:update / command_output) shown in the
   *  trace row while running; superseded by the tool_result block on terminal. */
  progressText?: string;
  /** Wall-clock ms for the call, carried from the live ToolStreamEntry so the
   *  trace can show a real duration. LIVE TURNS ONLY — messages replayed from
   *  chat.history have no timing, and the trace renders no duration rather than
   *  a wrong one. */
  startedAt?: number;
  endedAt?: number;
  /** Distinguishes a real tool call from a non-tool activity pill
   *  (plan/patch/item/acp) that reuses this block shape via a synthetic id.
   *  Only useAgentChat's agent_activity branch sets "activity"; a real tool
   *  call carries no value here (read as "tool"). Activity entries already
   *  carry a human label in `name` and must bypass the tool-name resolver
   *  entirely — see ToolActivityTrace. */
  kind?: "tool" | "activity";
  // tool_result-flavored
  tool_use_id?: string;
  toolUseId?: string;
  toolCallId?: string;
  content?: unknown;
  result?: unknown;
  isError?: boolean;
  // image-flavored (after upstream's chat-display-projection)
  omitted?: boolean;
  bytes?: number;
}

/** A whole message from chat.history or synthesized at chat.final. */
export interface HistoryMessage {
  role?: string;
  /** THREE shapes are live on the wire, all meaning "this message's body":
   *
   *  - `ContentBlock[]` — any turn carrying structured parts (thinking,
   *    tool calls, media).
   *  - `string` — OpenClaw persists a plain-text turn with `content` as a
   *    bare string (`buildPersistedUserTurnMessage`,
   *    `src/sessions/user-turn-transcript.ts`) and `chat.history` returns it
   *    verbatim; its own display projection reads `typeof content === "string"`
   *    first (`src/gateway/chat-display-projection.ts`). Every message the
   *    user actually types arrives this way.
   *  - absent, with the body on the sibling `text` field — older rows and
   *    cron run transcripts.
   *
   *  Never branch on `Array.isArray(content)` alone to decide whether a
   *  message has a body: that reads the string shape as "empty" and is
   *  exactly what silently hid every user bubble on a history refetch. Go
   *  through `resolveMessageBlocks`. */
  content?: ContentBlock[] | string;
  text?: string;
  timestamp?: number;
  runId?: string;
  toolCallId?: string;
  tool_call_id?: string;
  toolName?: string;
  tool_name?: string;
  /** Standalone toolResult-role rows (OpenClaw session store) carry error
   *  state at message level. */
  isError?: boolean;
  errorCode?: string;
  __openclaw?: { kind?: string };
}

// ---------------------------------------------------------------------------
// Block predicates
// ---------------------------------------------------------------------------

const TEXT_TYPES = new Set(["text", "output_text", "input_text"]);
const TOOL_USE_TYPES = new Set(["toolcall", "tool_call", "tooluse", "tool_use"]);
const TOOL_RESULT_TYPES = new Set(["toolresult", "tool_result"]);

function lowerType(b: unknown): string {
  if (!b || typeof b !== "object") return "";
  const t = (b as { type?: unknown }).type;
  return typeof t === "string" ? t.toLowerCase() : "";
}

function lowerRole(m: HistoryMessage): string {
  return typeof m.role === "string" ? m.role.toLowerCase() : "";
}

export function isTextBlock(b: unknown): boolean {
  return TEXT_TYPES.has(lowerType(b));
}

export function isThinkingBlock(b: unknown): boolean {
  return lowerType(b) === "thinking";
}

export function isToolUseBlock(b: unknown): boolean {
  return TOOL_USE_TYPES.has(lowerType(b));
}

export function isToolResultBlock(b: unknown): boolean {
  return TOOL_RESULT_TYPES.has(lowerType(b));
}

/** True for a message role that carries tool results rather than prose. */
export function isToolResultRole(role: string): boolean {
  return role === "tool" || role === "toolresult" || role === "tool_result";
}

/**
 * Normalize a message's body to a block array, collapsing the three shapes
 * `chat.history` returns for the same logical thing (see `HistoryMessage.content`):
 * a block array, a bare `content` string, or a top-level `text` field.
 *
 * This is the ONLY correct way to ask "what is in this message". Reading
 * `content` as an array and treating everything else as empty silently
 * discards every plain-text turn — which is how the string shape, the one
 * OpenClaw persists for literally every message a user types, went unrendered.
 */
export function resolveMessageBlocks(message: {
  content?: ContentBlock[] | string;
  text?: string;
}): ContentBlock[] {
  const { content } = message;
  if (Array.isArray(content) && content.length > 0) return content;
  if (typeof content === "string" && content.length > 0) {
    return [{ type: "text", text: content }];
  }
  if (typeof message.text === "string" && message.text.length > 0) {
    return [{ type: "text", text: message.text }];
  }
  return [];
}

/**
 * Resolve the block-level tool_use id, accepting all FOUR naming variants.
 *
 * `toolCallId` is the one web was missing — a tool_result carrying only that
 * spelling failed to pair and rendered a result-less pill.
 */
export function resolveToolUseId(b: unknown): string | undefined {
  if (!b || typeof b !== "object") return undefined;
  const r = b as {
    id?: unknown;
    tool_use_id?: unknown;
    toolUseId?: unknown;
    toolCallId?: unknown;
  };
  if (typeof r.id === "string" && r.id.length > 0) return r.id;
  if (typeof r.tool_use_id === "string" && r.tool_use_id.length > 0) return r.tool_use_id;
  if (typeof r.toolUseId === "string" && r.toolUseId.length > 0) return r.toolUseId;
  if (typeof r.toolCallId === "string" && r.toolCallId.length > 0) return r.toolCallId;
  return undefined;
}

/** Resolve tool args, accepting all three naming variants (Anthropic uses `input`).
 *  Returns the value verbatim — callers that need a plain object narrow it. */
export function resolveToolArgs(b: unknown): unknown {
  if (!b || typeof b !== "object") return undefined;
  const r = b as { args?: unknown; arguments?: unknown; input?: unknown };
  if (r.args !== undefined) return r.args;
  if (r.arguments !== undefined) return r.arguments;
  if (r.input !== undefined) return r.input;
  return undefined;
}

/** Flatten a tool_result payload (string | block[] | anything) to plain text. */
export function extractResultText(b: ContentBlock): string {
  const c = b.content ?? b.result;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c
      .map((entry) =>
        entry && typeof entry === "object" && typeof (entry as ContentBlock).text === "string"
          ? ((entry as ContentBlock).text as string)
          : "",
      )
      .filter((t) => t.length > 0)
      .join("\n");
  }
  return "";
}

// ---------------------------------------------------------------------------
// Visibility filter — mirrors upstream's `shouldHideHistoryMessage`
// (ui/src/pages/chat/chat-history.ts in the fork checkout).
//
// The load-bearing property to preserve: upstream decides visibility on a
// message's EXTRACTED TEXT, never on whether `content` happens to be an array.
// ---------------------------------------------------------------------------

const SILENT_REPLY_TOKEN = "NO_REPLY";
const HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK";
const SYNTHETIC_REPAIR_TOKEN = "SYNTHETIC_TRANSCRIPT_REPAIR_RESULT";

// OpenClaw injects a post-exec "wrapper" message with role:"user" after a
// tool/exec finishes — an LLM-facing blob (exec footer, raw stdout, and a
// "continue the task" instruction) that has no business rendering as a user
// bubble. These three substrings are distinctive enough to detect the
// template without matching genuine user prose. Extend the list if a new
// upstream wrapper variant surfaces.
const SYNTHETIC_EXEC_WRAPPER_MARKERS = [
  "Exec finished (gateway id=",
  "Exact completion details:",
  "Continue the task if needed, then reply to the user",
] as const;

function extractFirstText(m: HistoryMessage): string {
  // A bare-string `content` IS the message body (see HistoryMessage.content).
  // Checked first, and load-bearing for isSyntheticExecWrapper below: OpenClaw's
  // post-exec wrapper messages arrive in exactly this shape, and until the
  // string case was handled they were only ever hidden as collateral damage of
  // isEmptyUserTextOnlyMessage's array check. With that check fixed, this is
  // the sole thing still keeping them out of the transcript.
  if (typeof m.content === "string") return m.content;
  if (typeof m.text === "string") return m.text;
  if (!Array.isArray(m.content)) return "";
  for (const b of m.content) {
    if (b && typeof b === "object" && typeof b.text === "string") {
      return b.text;
    }
  }
  return "";
}

export function isAssistantSilentReply(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "assistant") return false;
  return extractFirstText(m).trim() === SILENT_REPLY_TOKEN;
}

export function isAssistantHeartbeatAck(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "assistant") return false;
  return extractFirstText(m).trim() === HEARTBEAT_OK_TOKEN;
}

export function isSyntheticTranscriptRepairToolResult(m: HistoryMessage): boolean {
  if (!isToolResultRole(lowerRole(m))) return false;
  if (!Array.isArray(m.content)) return false;
  for (const b of m.content) {
    if (!b || typeof b !== "object") continue;
    const c = b.content ?? b.result;
    if (typeof c === "string" && c.includes(SYNTHETIC_REPAIR_TOKEN)) return true;
  }
  return false;
}

export function isEmptyUserTextOnlyMessage(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "user") return false;
  // Decide on the message's TEXT, never on the shape of its container.
  //
  // This predicate used to open with `if (!Array.isArray(m.content)) return true`,
  // which reads OpenClaw's bare-string `content` — the shape it persists for
  // every plain-text turn — as "empty" and hid it. That silently deleted the
  // user's own words from every history refetch, and made the server-dispatched
  // onboarding kickoff invisible from the very first paint (it has no optimistic
  // local bubble to mask the loss, so Friday's transcript opened on her first
  // tool call with nothing to explain what she was answering).
  if (typeof m.content === "string") return m.content.trim().length === 0;
  // No block array: the body may still be on the sibling `text` field.
  if (!Array.isArray(m.content) || m.content.length === 0) {
    return (m.text ?? "").trim().length === 0;
  }
  // Reject if any non-text block exists (attachments, images, etc.). Type
  // matching is case-insensitive like every other block predicate here — web's
  // copy compared exact-case and was the lone outlier.
  const allTextBlocks = m.content.every((b) => isTextBlock(b));
  if (!allTextBlocks) return false;
  // All text blocks must be empty after trim.
  return m.content.every(
    (b) => typeof b.text !== "string" || b.text.trim().length === 0,
  );
}

export function isSyntheticExecWrapper(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "user") return false;
  const text = extractFirstText(m);
  if (!text) return false;
  return SYNTHETIC_EXEC_WRAPPER_MARKERS.some((marker) => text.includes(marker));
}

export function shouldHideHistoryMessage(m: HistoryMessage): boolean {
  return (
    isAssistantSilentReply(m) ||
    isAssistantHeartbeatAck(m) ||
    isSyntheticTranscriptRepairToolResult(m) ||
    isEmptyUserTextOnlyMessage(m) ||
    isSyntheticExecWrapper(m)
  );
}
