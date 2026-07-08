// chat.history → ChatItem[] projection (sessions contract §2).
//
// Ports the web's `hooks/internal/chatHistoryFilters.ts` (itself a mirror of
// upstream OpenClaw's `shouldHideHistoryMessage`) plus the tool_use ↔
// tool_result pairing from `hooks/internal/toolBlockExtract.ts`, flattened
// into the mobile screen's ChatItem list model.

import type { ChatItem } from "../components/chat/chatItems";

// ---------------------------------------------------------------------------
// Wire shapes (contract §2, mirrors web hooks/internal/types.ts)
// ---------------------------------------------------------------------------

export interface ContentBlock {
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
  // tool_result-flavored
  tool_use_id?: string;
  toolUseId?: string;
  toolCallId?: string;
  content?: unknown;
  result?: unknown;
  isError?: boolean;
}

export interface HistoryMessage {
  role?: string;
  content?: ContentBlock[];
  text?: string;
  timestamp?: number;
  runId?: string;
  toolName?: string;
  tool_name?: string;
  __openclaw?: { kind?: string };
}

// ---------------------------------------------------------------------------
// Projection filter — the 5-rule shouldHideHistoryMessage port
// ---------------------------------------------------------------------------

const SILENT_REPLY_TOKEN = "NO_REPLY";
const HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK";
const SYNTHETIC_REPAIR_TOKEN = "SYNTHETIC_TRANSCRIPT_REPAIR_RESULT";

// OpenClaw injects a post-exec "wrapper" message with role:"user" after a
// tool/exec finishes — an LLM-facing blob that has no business rendering as
// a user bubble. These substrings detect the template without matching
// genuine user prose.
const SYNTHETIC_EXEC_WRAPPER_MARKERS = [
  "Exec finished (gateway id=",
  "Exact completion details:",
  "Continue the task if needed, then reply to the user",
] as const;

const TEXT_TYPES = new Set(["text", "output_text", "input_text"]);
const TOOL_USE_TYPES = new Set(["toolcall", "tool_call", "tooluse", "tool_use"]);
const TOOL_RESULT_TYPES = new Set(["toolresult", "tool_result"]);

function lowerType(b: ContentBlock): string {
  return typeof b.type === "string" ? b.type.toLowerCase() : "";
}

function lowerRole(m: HistoryMessage): string {
  return typeof m.role === "string" ? m.role.toLowerCase() : "";
}

function extractFirstText(m: HistoryMessage): string {
  if (typeof m.text === "string") return m.text;
  if (!Array.isArray(m.content)) return "";
  for (const b of m.content) {
    if (b && typeof b === "object" && typeof b.text === "string") {
      return b.text;
    }
  }
  return "";
}

function isAssistantSilentReply(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "assistant") return false;
  return extractFirstText(m).trim() === SILENT_REPLY_TOKEN;
}

function isAssistantHeartbeatAck(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "assistant") return false;
  return extractFirstText(m).trim() === HEARTBEAT_OK_TOKEN;
}

function isSyntheticTranscriptRepairToolResult(m: HistoryMessage): boolean {
  const role = lowerRole(m);
  if (role !== "toolresult" && role !== "tool_result" && role !== "tool") return false;
  if (!Array.isArray(m.content)) return false;
  for (const b of m.content) {
    if (!b || typeof b !== "object") continue;
    const c = b.content ?? b.result;
    if (typeof c === "string" && c.includes(SYNTHETIC_REPAIR_TOKEN)) return true;
  }
  return false;
}

function isEmptyUserTextOnlyMessage(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "user") return false;
  if (!Array.isArray(m.content) || m.content.length === 0) return true;
  // Reject if any non-text block exists (attachments, images, etc.)
  const allTextBlocks = m.content.every(
    (b) => b && typeof b === "object" && TEXT_TYPES.has(lowerType(b)),
  );
  if (!allTextBlocks) return false;
  // All text blocks must be empty after trim.
  return m.content.every(
    (b) => typeof b.text !== "string" || b.text.trim().length === 0,
  );
}

function isSyntheticExecWrapper(m: HistoryMessage): boolean {
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

// ---------------------------------------------------------------------------
// Block helpers
// ---------------------------------------------------------------------------

/** Resolve the block-level tool_use id, accepting all three naming variants. */
function resolveToolUseId(b: ContentBlock): string | undefined {
  if (typeof b.id === "string" && b.id.length > 0) return b.id;
  if (typeof b.tool_use_id === "string" && b.tool_use_id.length > 0) return b.tool_use_id;
  if (typeof b.toolUseId === "string" && b.toolUseId.length > 0) return b.toolUseId;
  if (typeof b.toolCallId === "string" && b.toolCallId.length > 0) return b.toolCallId;
  return undefined;
}

/** Resolve tool args, accepting all three naming variants (Anthropic uses `input`). */
function resolveToolArgs(b: ContentBlock): Record<string, unknown> | undefined {
  const raw = b.args !== undefined ? b.args : b.arguments !== undefined ? b.arguments : b.input;
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  return undefined;
}

/** Flatten a tool_result payload (string | block[] | anything) to plain text. */
function extractResultText(b: ContentBlock): string {
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
// History → ChatItem[]
// ---------------------------------------------------------------------------

export interface ProjectedHistory {
  items: ChatItem[];
  /** runIds observed on history messages — the live-item dedupe set. */
  runIds: Set<string>;
  /** Text of the last user message — mid-run user-bubble dedupe (contract §2). */
  lastUserText: string | null;
  truncated: boolean;
}

/**
 * Project a chat.history payload into the chat screen's ChatItem list:
 * apply the 5-rule visibility filter, then flatten each surviving message —
 * text blocks become user/assistant items, tool_use blocks become tool items
 * (paired with tool_result blocks found in the same or later messages).
 * Messages arrive oldest→newest and are rendered as-is (no client sort).
 */
export function projectHistoryToChatItems(payload: unknown): ProjectedHistory {
  const body = (payload ?? {}) as { messages?: unknown; truncated?: boolean };
  const raw: HistoryMessage[] = Array.isArray(body.messages)
    ? (body.messages as HistoryMessage[])
    : [];

  const items: ChatItem[] = [];
  const runIds = new Set<string>();
  let lastUserText: string | null = null;
  // tool_use id → projected tool item, for result attachment (results may
  // live in the same assistant message or a separate tool-role message).
  const toolItemsByUseId = new Map<string, ChatItem>();

  raw.forEach((m, msgIdx) => {
    if (!m || typeof m !== "object") return;
    if (typeof m.runId === "string" && m.runId.length > 0) runIds.add(m.runId);
    if (shouldHideHistoryMessage(m)) return;
    // Compaction markers are dividers on web; mobile has no divider row yet.
    if (m.__openclaw?.kind === "compaction") return;

    const role = lowerRole(m);
    const blocks = Array.isArray(m.content) ? m.content : [];

    // Tool-result-role messages only attach results — they emit no items.
    if (role === "tool" || role === "toolresult" || role === "tool_result") {
      for (const b of blocks) {
        if (!b || typeof b !== "object" || !TOOL_RESULT_TYPES.has(lowerType(b))) continue;
        attachResult(toolItemsByUseId, b);
      }
      return;
    }

    if (role === "user") {
      const text =
        typeof m.text === "string" && m.text.trim().length > 0
          ? m.text
          : blocks
              .filter((b) => b && TEXT_TYPES.has(lowerType(b)) && typeof b.text === "string")
              .map((b) => b.text as string)
              .join("\n\n");
      if (text.trim().length === 0) return;
      items.push({
        id: `h:${msgIdx}:user`,
        role: "user",
        text,
        runId: m.runId,
      });
      lastUserText = text;
      return;
    }

    if (role !== "assistant") return;

    // Assistant: walk blocks in order — accumulate text, emit tool items at
    // tool_use blocks, attach any inline tool_result blocks.
    let buffer = "";
    let textOrdinal = 0;
    const flush = () => {
      if (buffer.trim().length === 0) {
        buffer = "";
        return;
      }
      items.push({
        id: `h:${msgIdx}:t${textOrdinal}`,
        role: "assistant",
        text: buffer,
        runId: m.runId,
      });
      textOrdinal += 1;
      buffer = "";
    };

    if (blocks.length === 0 && typeof m.text === "string") {
      buffer = m.text;
    }
    blocks.forEach((b, blockIdx) => {
      if (!b || typeof b !== "object") return;
      const type = lowerType(b);
      if (TEXT_TYPES.has(type) && typeof b.text === "string") {
        buffer = buffer.length > 0 ? `${buffer}\n\n${b.text}` : b.text;
        return;
      }
      if (TOOL_USE_TYPES.has(type)) {
        flush();
        const name = typeof b.name === "string" && b.name.length > 0 ? b.name : "tool";
        const item: ChatItem = {
          id: `h:${msgIdx}:b${blockIdx}`,
          role: "tool",
          text: `⚙ ${name}`,
          tool: name,
          args: resolveToolArgs(b),
          // History tools are settled by definition; errors flip on the
          // matching tool_result's isError.
          toolStatus: "done",
          runId: m.runId,
        };
        const useId = resolveToolUseId(b);
        if (useId) toolItemsByUseId.set(useId, item);
        items.push(item);
        return;
      }
      if (TOOL_RESULT_TYPES.has(type)) {
        attachResult(toolItemsByUseId, b);
      }
      // thinking/image/other blocks: not rendered on mobile v1.
    });
    flush();
  });

  return { items, runIds, lastUserText, truncated: body.truncated === true };
}

/** Mutate the paired tool item with this result block's text/error status. */
function attachResult(map: Map<string, ChatItem>, b: ContentBlock): void {
  const useId = resolveToolUseId(b);
  if (!useId) return;
  const item = map.get(useId);
  if (!item) return;
  const text = extractResultText(b);
  if (text.length > 0) item.result = text;
  if (b.isError === true) item.toolStatus = "error";
}
