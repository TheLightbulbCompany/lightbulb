// apps/frontend/src/hooks/internal/toolResultMerge.ts
//
// chat.history returns OpenClaw's session-store transcript, where a tool
// result is a STANDALONE message — `{ role: "toolResult", toolCallId,
// toolName, content: [{type:"text", …}], isError? }` — following the
// assistant message that carries the matching `toolCall` block. Upstream's
// control UI hides these rows by default (chat-thread.ts: `if
// (!showToolCalls && isToolResult) continue`); our MessageBlocks walked
// their `content` and, because the blocks are type "text", rendered the raw
// tool output (web_search JSON envelopes, SKILL.md bodies, exec stdout) as
// assistant PROSE.
//
// This projection folds each standalone tool-result message into a
// synthetic `tool_result` content block appended to the nearest preceding
// assistant message holding the matching un-resolved `toolCall` block —
// exactly the in-message shape the live fold path
// (useAgentChat.foldLiveTurnIntoContent) produces — so restored history
// renders the same collapsed, expandable ToolPill the live stream shows. An
// orphan result (its toolCall lives on an older, unfetched page, or the id
// is missing) is rewritten into a self-contained completed pill instead of
// being dropped, so its output stays reachable without ever rendering as
// prose.

import type { ContentBlock, HistoryMessage } from "./types";
import {
  isToolResultBlock,
  isToolUseBlock,
  resolveToolUseId,
} from "./toolBlockExtract";

const TOOL_RESULT_ROLES = new Set(["toolresult", "tool_result", "tool"]);

export function isStandaloneToolResultMessage(m: HistoryMessage): boolean {
  const role = typeof m.role === "string" ? m.role.toLowerCase() : "";
  return TOOL_RESULT_ROLES.has(role);
}

function resolveMessageToolCallId(m: HistoryMessage): string | undefined {
  if (typeof m.toolCallId === "string" && m.toolCallId.length > 0) return m.toolCallId;
  if (typeof m.tool_call_id === "string" && m.tool_call_id.length > 0) return m.tool_call_id;
  if (Array.isArray(m.content)) {
    for (const b of m.content) {
      const id = resolveToolUseId(b);
      if (id) return id;
    }
  }
  return undefined;
}

/** The result payload ToolPill's renderToolOutput understands: the original
 * block array when present (it extracts text + omitted-blob markers), else
 * the flat text field. */
function resolveResultContent(m: HistoryMessage): unknown {
  if (Array.isArray(m.content) && m.content.length > 0) return m.content;
  if (typeof m.text === "string") return m.text;
  return "";
}

export function mergeStandaloneToolResults(
  messages: HistoryMessage[],
): HistoryMessage[] {
  const out: HistoryMessage[] = [];
  for (const m of messages) {
    if (!isStandaloneToolResultMessage(m)) {
      out.push(m);
      continue;
    }

    const id = resolveMessageToolCallId(m);
    const isError = (m as { isError?: unknown }).isError === true;
    const resultContent = resolveResultContent(m);

    // Nearest preceding assistant message with the matching, still-unresolved
    // toolCall block. Stop at the first message that CARRIES the id — if it
    // already has a result (a re-delivered frame), fall through to the
    // orphan path rather than double-appending.
    let targetIndex = -1;
    if (id) {
      for (let i = out.length - 1; i >= 0; i--) {
        const cand = out[i];
        const role = typeof cand.role === "string" ? cand.role.toLowerCase() : "";
        if (role !== "assistant") continue;
        const blocks = Array.isArray(cand.content) ? cand.content : [];
        const hasUse = blocks.some(
          (b) => isToolUseBlock(b) && resolveToolUseId(b) === id,
        );
        if (!hasUse) continue;
        const hasResult = blocks.some(
          (b) => isToolResultBlock(b) && resolveToolUseId(b) === id,
        );
        if (!hasResult) targetIndex = i;
        break;
      }
    }

    if (targetIndex >= 0) {
      const target = out[targetIndex];
      const resultBlock: ContentBlock = {
        type: "tool_result",
        tool_use_id: id,
        content: resultContent,
        ...(isError ? { isError: true } : {}),
      };
      out[targetIndex] = {
        ...target,
        content: [...(target.content ?? []), resultBlock],
      };
      continue;
    }

    // Orphan: synthesize a completed pill (tool_use + tool_result pair) so
    // MessageBlocks renders the standard chip instead of prose.
    const orphanId = id ?? `orphan-tool-${out.length}`;
    const name = m.toolName ?? m.tool_name ?? "tool";
    out.push({
      role: "assistant",
      ...(m.timestamp !== undefined ? { timestamp: m.timestamp } : {}),
      ...(m.runId !== undefined ? { runId: m.runId } : {}),
      content: [
        { type: "tool_use", id: orphanId, name },
        {
          type: "tool_result",
          tool_use_id: orphanId,
          content: resultContent,
          ...(isError ? { isError: true } : {}),
        },
      ],
    });
  }
  return out;
}
