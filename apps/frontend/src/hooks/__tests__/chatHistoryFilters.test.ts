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

  // The shape OpenClaw ACTUALLY persists for a plain-text turn:
  // `buildPersistedUserTurnMessage` (src/sessions/user-turn-transcript.ts)
  // stores `content` as a bare string, and chat.history returns it verbatim.
  // Reading a non-array `content` as "empty" hid every user message on a
  // history refetch.
  it("returns false for user whose content is a bare string", () => {
    const m: HistoryMessage = { role: "user", content: "Hello" };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(false);
  });

  it("returns true for user whose bare-string content is only whitespace", () => {
    const m: HistoryMessage = { role: "user", content: "   " };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(true);
  });

  it("returns false for user carrying its body on the top-level text field", () => {
    const m: HistoryMessage = { role: "user", text: "Hello" };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(false);
  });

  it("returns true for user with neither content nor text", () => {
    const m: HistoryMessage = { role: "user" };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(true);
  });
});

describe("isSyntheticExecWrapper", () => {
  const FULL_TEMPLATE = [
    "Exact completion details:",
    "Exec finished (gateway id=3f9c1a2e-7b44-4d51-9c0a-1e2f3a4b5c6d, session=friday-main, code 0)",
    "total 8",
    "drwxr-xr-x  2 node node 4096 May 21 00:00 .",
    "-rw-r--r--  1 node node   42 May 21 00:00 notes.md",
    "Continue the task if needed, then reply to the user in a helpful way.",
    "If it succeeded, share the relevant output.",
    "If it failed, explain what went wrong.",
  ].join("\n");

  it("hides the full leaked exec-wrapper template", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: FULL_TEMPLATE }],
    };
    expect(isSyntheticExecWrapper(m)).toBe(true);
  });

  it("matches the 'Exec finished (gateway id=' marker in isolation", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "Exec finished (gateway id=abc, session=x, code 0)" }],
    };
    expect(isSyntheticExecWrapper(m)).toBe(true);
  });

  it("matches the 'Exact completion details:' marker in isolation", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "Exact completion details: foo" }],
    };
    expect(isSyntheticExecWrapper(m)).toBe(true);
  });

  it("matches the 'Continue the task if needed' marker in isolation", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "Continue the task if needed, then reply to the user" }],
    };
    expect(isSyntheticExecWrapper(m)).toBe(true);
  });

  it("does NOT hide normal user text", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "List all the files in your workspace" }],
    };
    expect(isSyntheticExecWrapper(m)).toBe(false);
  });

  it("does NOT hide an assistant message that merely describes the markers", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [
        {
          type: "text",
          text: "When a tool runs, you'll see an 'Exec finished (gateway id=...)' line in the transcript.",
        },
      ],
    };
    expect(isSyntheticExecWrapper(m)).toBe(false);
  });

  // Load-bearing: exec wrappers are role:"user" and arrive with bare-string
  // content, exactly like a typed message. They used to be hidden only as
  // collateral damage of isEmptyUserTextOnlyMessage's array check — now that
  // string content is rendered, THIS is the only thing keeping them out.
  it("matches the marker when content is a bare string", () => {
    const m: HistoryMessage = { role: "user", content: FULL_TEMPLATE };
    expect(isSyntheticExecWrapper(m)).toBe(true);
    expect(shouldHideHistoryMessage(m)).toBe(true);
  });
});

describe("shouldHideHistoryMessage", () => {
  it("hides the synthetic exec-wrapper user message", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "Exec finished (gateway id=abc, session=x, code 0)" }],
    };
    expect(shouldHideHistoryMessage(m)).toBe(true);
  });
});

describe("projectChatHistoryMessages", () => {
  const firstText = (m: HistoryMessage): string | undefined =>
    Array.isArray(m.content) ? m.content[0]?.text : undefined;

  it("removes hidden messages while preserving order", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "assistant", content: [{ type: "text", text: "NO_REPLY" }] },
      { role: "assistant", content: [{ type: "text", text: "hello back" }] },
    ];
    const result = projectChatHistoryMessages(messages);
    expect(result).toHaveLength(2);
    expect(firstText(result[0])).toBe("hi");
    expect(firstText(result[1])).toBe("hello back");
  });

  // Regression, verbatim from the dev transcript that exposed this
  // (agents/<friday>/agent/openclaw-agent.sqlite, table transcript_events):
  // the server-dispatched onboarding kickoff persists as role:"user" with
  // bare-string content, immediately followed by Friday's first assistant
  // turn. The projection used to drop the user row and keep the assistant
  // one, so the transcript opened on `update_plan` with nothing above it.
  it("keeps the server-dispatched onboarding kickoff above Friday's first turn", () => {
    const KICKOFF =
      "I'm starting a subscription coffee roastery that ships single-origin beans to " +
      "home brewers — it's for home brewers who care about origin and freshness. Dig " +
      "into the business and market, write it up, and put my three best next moves in " +
      "the backlog — don't start any of them yet, just tell me which to tackle first. " +
      "I'm watching.";
    const messages: HistoryMessage[] = [
      { role: "user", content: KICKOFF, timestamp: 1785009807010 },
      {
        role: "assistant",
        content: [
          { type: "thinking", thinking: "The user wants me to research…" },
          { type: "toolCall", id: "tooluse_1", name: "update_plan", arguments: {} },
        ],
        timestamp: 1785009818955,
      },
    ];
    const result = projectChatHistoryMessages(messages);
    expect(result).toHaveLength(2);
    expect(result[0].role).toBe("user");
    expect(result[0].content).toBe(KICKOFF);
    expect(result[1].role).toBe("assistant");
  });
});
