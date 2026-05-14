// apps/frontend/src/hooks/internal/toolBlockExtract.ts
//
// Content-block predicates and tool_use ↔ tool_result pairing.
// Ported from upstream OpenClaw's `~/Desktop/openclaw/src/chat/
// tool-content.ts` and `~/Desktop/openclaw/ui/src/ui/chat/tool-cards.ts`.

import type { ContentBlock } from "./types";

const TEXT_TYPES = new Set(["text", "output_text", "input_text"]);
const TOOL_USE_TYPES = new Set(["toolcall", "tool_call", "tooluse", "tool_use"]);
const TOOL_RESULT_TYPES = new Set(["toolresult", "tool_result"]);

function lowerType(b: unknown): string {
  if (!b || typeof b !== "object") return "";
  const t = (b as { type?: unknown }).type;
  return typeof t === "string" ? t.toLowerCase() : "";
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

/** Resolve the block-level tool_use id, accepting all three naming variants. */
export function resolveToolUseId(b: unknown): string | undefined {
  if (!b || typeof b !== "object") return undefined;
  const r = b as { id?: unknown; tool_use_id?: unknown; toolUseId?: unknown };
  if (typeof r.id === "string" && r.id.length > 0) return r.id;
  if (typeof r.tool_use_id === "string" && r.tool_use_id.length > 0) return r.tool_use_id;
  if (typeof r.toolUseId === "string" && r.toolUseId.length > 0) return r.toolUseId;
  return undefined;
}

/** Resolve tool args, accepting all three naming variants (Anthropic uses `input`). */
export function resolveToolArgs(b: unknown): unknown {
  if (!b || typeof b !== "object") return undefined;
  const r = b as { args?: unknown; arguments?: unknown; input?: unknown };
  if (r.args !== undefined) return r.args;
  if (r.arguments !== undefined) return r.arguments;
  if (r.input !== undefined) return r.input;
  return undefined;
}

export interface ToolBlockPair {
  toolUseId: string;
  toolUseBlock: ContentBlock;
  toolResultBlock?: ContentBlock;
}

/**
 * Pair tool_use blocks with their matching tool_result blocks within a single
 * message's content array. Pairing is by id (block-level). Orphan tool_results
 * (no matching tool_use) are dropped.
 *
 * Source: upstream's `tool-cards.ts:73-185` does positional-within-message
 * pairing; we use id-based to be more robust to reordering. Functionally
 * equivalent for messages that have one tool_use + one tool_result in order.
 */
export function pairToolBlocks(blocks: ContentBlock[]): ToolBlockPair[] {
  const pairs: ToolBlockPair[] = [];
  const useById = new Map<string, ContentBlock>();
  const useIdOrder: string[] = [];

  for (const b of blocks) {
    if (isToolUseBlock(b)) {
      const id = resolveToolUseId(b);
      if (!id) continue;
      useById.set(id, b);
      useIdOrder.push(id);
    }
  }

  for (const b of blocks) {
    if (!isToolResultBlock(b)) continue;
    const targetId = resolveToolUseId(b);
    if (!targetId) continue;
    const useBlock = useById.get(targetId);
    if (!useBlock) continue;
    pairs.push({ toolUseId: targetId, toolUseBlock: useBlock, toolResultBlock: b });
    useById.delete(targetId);
  }

  // Any remaining tool_use blocks without a result still appear (status: running/done unknown).
  for (const id of useIdOrder) {
    if (useById.has(id)) {
      pairs.push({ toolUseId: id, toolUseBlock: useById.get(id)! });
    }
  }

  return pairs;
}
