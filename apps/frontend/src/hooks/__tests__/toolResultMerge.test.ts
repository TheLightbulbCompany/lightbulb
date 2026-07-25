// apps/frontend/src/hooks/__tests__/toolResultMerge.test.ts
import { describe, it, expect } from "vitest";
import {
  isStandaloneToolResultMessage,
  mergeStandaloneToolResults,
} from "../internal/toolResultMerge";
import { projectChatHistoryMessages } from "../internal/chatHistoryFilters";
import { resolveMessageBlocks } from "../internal/toolBlockExtract";
import type { HistoryMessage } from "../internal/types";

/** `content` is a union (block array | bare string) — normalize before indexing. */
const blocksOf = (m: HistoryMessage) => resolveMessageBlocks(m);

const WEB_SEARCH_RESULT_TEXT =
  '{"kind":"answer","provider":"parallel","answer":"<<<EXTERNAL_UNTRUSTED_CONTENT id=\\"ab12\\">>>\\nSource: Web Search\\n---\\nstuff\\n<<<END_EXTERNAL_UNTRUSTED_CONTENT id=\\"ab12\\">>>","externalContent":{"untrusted":true}}';

function assistantWithToolCall(id: string): HistoryMessage {
  return {
    role: "assistant",
    content: [
      { type: "text", text: "Let me look that up." },
      { type: "toolCall", id, name: "web_search", arguments: { query: "acme" } },
    ],
  };
}

function toolResultMessage(id: string, text: string, extra?: Partial<HistoryMessage>): HistoryMessage {
  return {
    role: "toolResult",
    toolCallId: id,
    toolName: "web_search",
    content: [{ type: "text", text }],
    ...extra,
  };
}

describe("isStandaloneToolResultMessage", () => {
  it("matches toolResult / tool_result / tool roles, case-insensitively", () => {
    expect(isStandaloneToolResultMessage({ role: "toolResult" })).toBe(true);
    expect(isStandaloneToolResultMessage({ role: "tool_result" })).toBe(true);
    expect(isStandaloneToolResultMessage({ role: "tool" })).toBe(true);
    expect(isStandaloneToolResultMessage({ role: "assistant" })).toBe(false);
    expect(isStandaloneToolResultMessage({ role: "user" })).toBe(false);
  });
});

describe("mergeStandaloneToolResults", () => {
  it("folds a toolResult row into the preceding assistant message's matching toolCall", () => {
    const merged = mergeStandaloneToolResults([
      assistantWithToolCall("call-1"),
      toolResultMessage("call-1", WEB_SEARCH_RESULT_TEXT),
      { role: "assistant", content: [{ type: "text", text: "Here's what I found." }] },
    ]);

    // The standalone row is gone…
    expect(merged).toHaveLength(2);
    expect(merged.every((m) => m.role === "assistant")).toBe(true);
    // …and its payload now lives as a tool_result block paired with the call,
    // which MessageBlocks renders as the collapsed pill (never prose).
    const blocks = blocksOf(merged[0]);
    const result = blocks.find((b) => b.type === "tool_result");
    expect(result).toBeDefined();
    expect(result!.tool_use_id).toBe("call-1");
    expect(result!.content).toEqual([{ type: "text", text: WEB_SEARCH_RESULT_TEXT }]);
  });

  it("carries the error flag onto the folded tool_result block", () => {
    const merged = mergeStandaloneToolResults([
      assistantWithToolCall("call-1"),
      toolResultMessage("call-1", "boom", { isError: true }),
    ]);
    const result = blocksOf(merged[0]).find((b) => b.type === "tool_result");
    expect(result!.isError).toBe(true);
  });

  it("rewrites an orphan toolResult (no matching call in view) into a completed pill pair", () => {
    // e.g. the matching toolCall lives on an older, unfetched history page.
    const merged = mergeStandaloneToolResults([
      toolResultMessage("call-9", "SKILL.md body …"),
      { role: "assistant", content: [{ type: "text", text: "Done reading." }] },
    ]);

    expect(merged).toHaveLength(2);
    const orphan = merged[0];
    expect(orphan.role).toBe("assistant");
    const use = blocksOf(orphan).find((b) => b.type === "tool_use");
    const result = blocksOf(orphan).find((b) => b.type === "tool_result");
    expect(use).toBeDefined();
    expect(use!.name).toBe("web_search");
    expect(result!.tool_use_id).toBe(use!.id);
  });

  it("does not double-append when the assistant message already has that result", () => {
    const settled: HistoryMessage = {
      role: "assistant",
      content: [
        { type: "toolCall", id: "call-1", name: "exec" },
        { type: "tool_result", tool_use_id: "call-1", content: "already here" },
      ],
    };
    const merged = mergeStandaloneToolResults([
      settled,
      toolResultMessage("call-1", "re-delivered"),
    ]);
    // The re-delivered row falls to the orphan path instead of duplicating.
    expect(blocksOf(merged[0]).filter((b) => b.type === "tool_result")).toHaveLength(1);
    expect(merged).toHaveLength(2);
  });

  it("leaves user/assistant messages untouched", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "assistant", content: [{ type: "text", text: "hello" }] },
    ];
    expect(mergeStandaloneToolResults(messages)).toEqual(messages);
  });
});

describe("projectChatHistoryMessages (merge integration)", () => {
  it("drops synthetic repair tool-results outright AND folds real ones", () => {
    const projected = projectChatHistoryMessages([
      assistantWithToolCall("call-1"),
      toolResultMessage("call-1", "real output"),
      // Shaped the way isSyntheticTranscriptRepairToolResult matches: a
      // tool-result block whose `content` string carries the repair token.
      {
        role: "toolResult",
        content: [
          { type: "tool_result", content: "SYNTHETIC_TRANSCRIPT_REPAIR_RESULT" },
        ],
      } as HistoryMessage,
    ]);
    // No standalone tool-result rows survive projection…
    expect(projected.some((m) => m.role.toLowerCase() === "toolresult")).toBe(false);
    // …the repair row is gone entirely (filtered, not folded into a pill)…
    expect(projected).toHaveLength(1);
    // …and the real result is folded onto its assistant message.
    const result = blocksOf(projected[0]).find((b) => b.type === "tool_result");
    expect(result).toBeDefined();
    expect(result!.content).toEqual([{ type: "text", text: "real output" }]);
  });
});
