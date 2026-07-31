// chat.history → ChatItem[] projection (sessions contract §2).
//
// The OpenClaw semantics this used to hand-port — the wire shapes, the 5-rule
// visibility filter, the block/tool-id resolvers — now come from
// @isol8/shared (packages/shared/src/chat/history.ts), which web reads too.
// What stays here is the part that is genuinely mobile's: flattening a
// filtered transcript into this screen's ChatItem list model.

import {
  extractResultText,
  isTextBlock,
  isToolResultBlock,
  isToolResultRole,
  isToolUseBlock,
  resolveMessageBlocks,
  resolveToolArgs,
  resolveToolUseId,
  shouldHideHistoryMessage,
  type ContentBlock,
  type HistoryMessage,
} from "@isol8/shared";

import type { ChatItem } from "../components/chat/chatItems";

// Re-exported: the mobile projection's own tests and callers import these
// from here, and the wire types are part of this module's signature.
export { resolveMessageBlocks, shouldHideHistoryMessage };
export type { ContentBlock, HistoryMessage };

function lowerRole(m: HistoryMessage): string {
  return typeof m.role === "string" ? m.role.toLowerCase() : "";
}

/** Tool args narrowed to a plain object — ChatItem.args is a Record. */
function toolArgsRecord(b: ContentBlock): Record<string, unknown> | undefined {
  const raw = resolveToolArgs(b);
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  return undefined;
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
    const blocks = resolveMessageBlocks(m);

    // Tool-result-role messages only attach results — they emit no items.
    if (isToolResultRole(role)) {
      for (const b of blocks) {
        if (!b || typeof b !== "object" || !isToolResultBlock(b)) continue;
        attachResult(toolItemsByUseId, b);
      }
      return;
    }

    if (role === "user") {
      const text =
        typeof m.text === "string" && m.text.trim().length > 0
          ? m.text
          : blocks
              .filter((b) => b && isTextBlock(b) && typeof b.text === "string")
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
      if (isTextBlock(b) && typeof b.text === "string") {
        buffer = buffer.length > 0 ? `${buffer}\n\n${b.text}` : b.text;
        return;
      }
      if (isToolUseBlock(b)) {
        flush();
        const name = typeof b.name === "string" && b.name.length > 0 ? b.name : "tool";
        const item: ChatItem = {
          id: `h:${msgIdx}:b${blockIdx}`,
          role: "tool",
          text: `⚙ ${name}`,
          tool: name,
          args: toolArgsRecord(b),
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
      if (isToolResultBlock(b)) {
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
