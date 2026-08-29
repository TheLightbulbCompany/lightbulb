// apps/frontend/src/hooks/internal/toolBlockExtract.ts
//
// The block predicates and id/args resolvers moved to @lightbulb/shared
// (packages/shared/src/chat/history.ts) — mobile had its own copy, and the two
// had drifted (web's resolveToolUseId was missing the `toolCallId` spelling).
// Re-exported here so existing imports and tests are unaffected.
//
// `pairToolBlocks` stays web-side: it is web's within-message pairing model,
// and mobile pairs across messages with its own map. Nothing else uses it.

import type { ContentBlock } from "@lightbulb/shared";
import { isToolResultBlock, isToolUseBlock, resolveToolUseId } from "@lightbulb/shared";

export {
  isTextBlock,
  isThinkingBlock,
  isToolResultBlock,
  isToolUseBlock,
  resolveMessageBlocks,
  resolveToolArgs,
  resolveToolUseId,
} from "@lightbulb/shared";

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
 * Source: upstream's `tool-cards.ts` does positional-within-message
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
