import { describe, it, expect } from "vitest";
import { buildChatItems } from "../internal/buildChatItems";
import { groupBySide } from "../internal/groupBySide";
import type { HistoryMessage } from "../internal/types";

const baseState = {
  chatMessages: [] as HistoryMessage[],
  chatStream: null as string | null,
  chatStreamRunId: null as string | null,
  chatStreamThinking: null as string | null,
  chatStreamThinkingRunId: null as string | null,
  chatStreamSegments: [] as Array<{ text: string; ts: number }>,
  chatToolMessages: [] as HistoryMessage[],
  sessionKey: "agent:test:user",
};

describe("buildChatItems", () => {
  it("returns empty for empty state", () => {
    expect(buildChatItems(baseState)).toEqual([]);
  });

  it("emits one message item per chatMessages entry", () => {
    const msgs: HistoryMessage[] = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "assistant", content: [{ type: "text", text: "hello" }] },
    ];
    const items = buildChatItems({ ...baseState, chatMessages: msgs });
    expect(items).toHaveLength(2);
    expect(items[0].kind).toBe("message");
    expect(items[1].kind).toBe("message");
  });

  it("emits a divider for compaction-marked messages", () => {
    const msgs: HistoryMessage[] = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "system", __openclaw: { kind: "compaction" } },
      { role: "user", content: [{ type: "text", text: "hi2" }] },
    ];
    const items = buildChatItems({ ...baseState, chatMessages: msgs });
    expect(items).toHaveLength(3);
    expect(items[1].kind).toBe("divider");
    if (items[1].kind === "divider") {
      expect(items[1].label).toContain("compact");
    }
  });

  it("interleaves chatStreamSegments and chatToolMessages by index", () => {
    const segments = [
      { text: "before tool A", ts: 1 },
      { text: "between A and B", ts: 3 },
    ];
    const toolMsgs: HistoryMessage[] = [
      { role: "assistant", content: [{ type: "tool_use", id: "A", name: "exec" }] },
      { role: "assistant", content: [{ type: "tool_use", id: "B", name: "exec" }] },
    ];
    const items = buildChatItems({
      ...baseState,
      chatStreamSegments: segments,
      chatToolMessages: toolMsgs,
    });
    expect(items.map((i) => i.kind)).toEqual([
      "stream-segment",
      "message",
      "stream-segment",
      "message",
    ]);
  });

  it("appends live thinking item before live stream at the tail", () => {
    const items = buildChatItems({
      ...baseState,
      chatStreamThinking: "let me think",
      chatStreamThinkingRunId: "run-1",
      chatStream: "the answer is",
      chatStreamRunId: "run-1",
    });
    expect(items).toHaveLength(2);
    expect(items[0].kind).toBe("live-thinking");
    expect(items[1].kind).toBe("stream");
  });

  it("omits live items when their text is empty", () => {
    const items = buildChatItems({
      ...baseState,
      chatStream: "",
      chatStreamRunId: "run-1",
    });
    expect(items).toHaveLength(0);
  });
});

describe("groupBySide", () => {
  it("groups consecutive user items as user side", () => {
    const items = buildChatItems({
      ...baseState,
      chatMessages: [
        { role: "user", content: [{ type: "text", text: "a" }] },
        { role: "user", content: [{ type: "text", text: "b" }] },
      ],
    });
    const groups = groupBySide(items);
    expect(groups).toHaveLength(1);
    expect(groups[0].side).toBe("user");
    expect(groups[0].items).toHaveLength(2);
  });

  it("breaks groups on user/assistant role transitions", () => {
    const items = buildChatItems({
      ...baseState,
      chatMessages: [
        { role: "user", content: [{ type: "text", text: "a" }] },
        { role: "assistant", content: [{ type: "text", text: "b" }] },
        { role: "user", content: [{ type: "text", text: "c" }] },
      ],
    });
    const groups = groupBySide(items);
    expect(groups.map((g) => g.side)).toEqual(["user", "assistant", "user"]);
  });

  it("treats assistant + tool + stream items as one assistant group", () => {
    const items = buildChatItems({
      ...baseState,
      chatMessages: [
        { role: "assistant", content: [{ type: "text", text: "thinking..." }] },
      ],
      chatToolMessages: [
        { role: "assistant", content: [{ type: "tool_use", id: "A", name: "exec" }] },
      ],
      chatStreamSegments: [{ text: "more text", ts: 1 }],
      chatStream: "even more",
      chatStreamRunId: "r",
    });
    const groups = groupBySide(items);
    expect(groups).toHaveLength(1);
    expect(groups[0].side).toBe("assistant");
  });

  it("treats divider as its own side-less group with side='assistant'", () => {
    const items = buildChatItems({
      ...baseState,
      chatMessages: [
        { role: "user", content: [{ type: "text", text: "a" }] },
        { role: "system", __openclaw: { kind: "compaction" } },
        { role: "user", content: [{ type: "text", text: "b" }] },
      ],
    });
    const groups = groupBySide(items);
    expect(groups.length).toBeGreaterThan(1);
  });
});
