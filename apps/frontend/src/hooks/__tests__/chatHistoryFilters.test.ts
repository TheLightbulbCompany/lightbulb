// apps/frontend/src/hooks/__tests__/chatHistoryFilters.test.ts
import { describe, it, expect } from "vitest";
import {
  isAssistantSilentReply,
  isAssistantHeartbeatAck,
  isSyntheticTranscriptRepairToolResult,
  isEmptyUserTextOnlyMessage,
  isSyntheticExecWrapper,
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

describe("isSyntheticExecWrapper", () => {
  const FULL_LEAKED_TEMPLATE = [
    "Exact completion details:",
    "",
    "Exec finished (gateway id=abc-123-def, session=friday-task, code 0)",
    "",
    "file1.txt",
    "file2.txt",
    "",
    "Continue the task if needed, then reply to the user in a helpful way.",
    "If it succeeded, share the relevant output.",
    "If it failed, explain what went wrong.",
  ].join("\n");

  it("hides a user message containing the full leaked exec-wrapper template", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: FULL_LEAKED_TEMPLATE }],
    };
    expect(isSyntheticExecWrapper(m)).toBe(true);
  });

  it("matches the 'Exec finished (gateway id=' marker in isolation", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [
        { type: "text", text: "Exec finished (gateway id=xyz, session=s, code 0)" },
      ],
    };
    expect(isSyntheticExecWrapper(m)).toBe(true);
  });

  it("matches the 'Exact completion details:' marker in isolation", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "Exact completion details: foo bar" }],
    };
    expect(isSyntheticExecWrapper(m)).toBe(true);
  });

  it("matches the 'Continue the task if needed' marker in isolation", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [
        {
          type: "text",
          text: "Continue the task if needed, then reply to the user in a helpful way.",
        },
      ],
    };
    expect(isSyntheticExecWrapper(m)).toBe(true);
  });

  it("does not hide a normal user message", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "List the files in my workspace please" }],
    };
    expect(isSyntheticExecWrapper(m)).toBe(false);
  });

  it("does not hide an assistant message that merely describes the markers", () => {
    // Critical: docs/help content from the agent referencing these phrases
    // must NOT be filtered. Only user-role leaks are synthetic.
    const m: HistoryMessage = {
      role: "assistant",
      content: [
        {
          type: "text",
          text:
            "The string 'Exec finished (gateway id=' is what OpenClaw injects " +
            "after each tool run. We strip it because 'Continue the task if " +
            "needed, then reply to the user' is meta-prompting, not user input.",
        },
      ],
    };
    expect(isSyntheticExecWrapper(m)).toBe(false);
  });

  it("returns false for a user message with no text", () => {
    const m: HistoryMessage = { role: "user", content: [] };
    expect(isSyntheticExecWrapper(m)).toBe(false);
  });
});

describe("shouldHideHistoryMessage (integration)", () => {
  it("hides a synthetic exec-wrapper user message", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [
        { type: "text", text: "Exec finished (gateway id=abc, session=s, code 0)" },
      ],
    };
    expect(shouldHideHistoryMessage(m)).toBe(true);
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

  it("strips the synthetic exec-wrapper user bubble from the projection", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: [{ type: "text", text: "ls my workspace" }] },
      { role: "assistant", content: [{ type: "text", text: "Sure, running ls now." }] },
      {
        role: "user",
        content: [
          {
            type: "text",
            text:
              "Exact completion details:\n\nExec finished (gateway id=abc, " +
              "session=s, code 0)\n\nfile1.txt\n\nContinue the task if needed, " +
              "then reply to the user in a helpful way.",
          },
        ],
      },
      { role: "assistant", content: [{ type: "text", text: "Found file1.txt." }] },
    ];
    const result = projectChatHistoryMessages(messages);
    expect(result).toHaveLength(3);
    expect(result.map((m) => m.content?.[0].text)).toEqual([
      "ls my workspace",
      "Sure, running ls now.",
      "Found file1.txt.",
    ]);
  });
});
