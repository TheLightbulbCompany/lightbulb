// apps/frontend/src/hooks/__tests__/chatHistoryFilters.test.ts
import { describe, it, expect } from "vitest";
import {
  isAssistantSilentReply,
  isAssistantHeartbeatAck,
  isSyntheticTranscriptRepairToolResult,
  isEmptyUserTextOnlyMessage,
  shouldHideHistoryMessage,
  projectChatHistoryMessages,
} from "../internal/chatHistoryFilters";
import type { HistoryMessage } from "../internal/types";

describe("isAssistantSilentReply", () => {
  it("returns true when assistant text is NO_REPLY", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "NO_REPLY" }],
    };
    expect(isAssistantSilentReply(m)).toBe(true);
  });

  it("returns true with whitespace around NO_REPLY", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "  NO_REPLY  " }],
    };
    expect(isAssistantSilentReply(m)).toBe(true);
  });

  it("returns false for normal assistant text", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "Hello" }],
    };
    expect(isAssistantSilentReply(m)).toBe(false);
  });

  it("returns false for non-assistant role", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "NO_REPLY" }],
    };
    expect(isAssistantSilentReply(m)).toBe(false);
  });
});

describe("isAssistantHeartbeatAck", () => {
  it("returns true for assistant 'HEARTBEAT_OK' response", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "HEARTBEAT_OK" }],
    };
    expect(isAssistantHeartbeatAck(m)).toBe(true);
  });

  it("returns false for normal assistant text", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "Hello" }],
    };
    expect(isAssistantHeartbeatAck(m)).toBe(false);
  });
});

describe("isSyntheticTranscriptRepairToolResult", () => {
  it("returns true for tool message with synthetic-repair marker", () => {
    const m: HistoryMessage = {
      role: "toolResult",
      content: [{ type: "tool_result", content: "SYNTHETIC_TRANSCRIPT_REPAIR_RESULT" }],
    };
    expect(isSyntheticTranscriptRepairToolResult(m)).toBe(true);
  });

  it("returns false for normal tool result", () => {
    const m: HistoryMessage = {
      role: "toolResult",
      content: [{ type: "tool_result", content: "actual output" }],
    };
    expect(isSyntheticTranscriptRepairToolResult(m)).toBe(false);
  });
});

describe("isEmptyUserTextOnlyMessage", () => {
  it("returns true for user with only-whitespace text", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "   " }],
    };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(true);
  });

  it("returns true for user with no content blocks", () => {
    const m: HistoryMessage = { role: "user", content: [] };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(true);
  });

  it("returns false for user with non-empty text", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "Hello" }],
    };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(false);
  });

  it("returns false for user with non-text content (image attachment)", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "image", bytes: 1024 }],
    };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(false);
  });
});

describe("projectChatHistoryMessages", () => {
  it("removes hidden messages while preserving order", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "assistant", content: [{ type: "text", text: "NO_REPLY" }] },
      { role: "assistant", content: [{ type: "text", text: "hello back" }] },
    ];
    const result = projectChatHistoryMessages(messages);
    expect(result).toHaveLength(2);
    expect(result[0].content?.[0].text).toBe("hi");
    expect(result[1].content?.[0].text).toBe("hello back");
  });
});
