import { describe, it, expect } from "vitest";
import {
  adaptSessionMessages,
  projectSessionMessages,
} from "@/components/control/panels/cron/sessionMessageAdapter";

// After the flat-chat-timeline migration (plan 2026-05-13), the cron-run
// transcript adapter is two helpers:
//
//   - projectSessionMessages(raw): raw → HistoryMessage[] (hide-filter applied)
//   - adaptSessionMessages(raw, sessionKey): raw → RenderGroup[] (the shape
//     MessageList consumes)
//
// These tests pin both layers so the cron transcript stays in lockstep with
// the live-chat rendering pipeline.

describe("projectSessionMessages", () => {
  it("normalizes user/assistant turns and preserves content blocks", () => {
    const raw = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "assistant", content: [{ type: "text", text: "hello" }] },
    ];
    const projected = projectSessionMessages(raw);
    expect(projected).toHaveLength(2);
    expect(projected[0].role).toBe("user");
    expect(projected[0].content).toEqual([{ type: "text", text: "hi" }]);
    expect(projected[1].role).toBe("assistant");
    expect(projected[1].content).toEqual([{ type: "text", text: "hello" }]);
  });

  it("preserves thinking blocks inside content (rendered by MessageBlocks)", () => {
    const raw = [
      {
        role: "assistant",
        content: [
          { type: "thinking", text: "considering options" },
          { type: "text", text: "here you go" },
        ],
      },
    ];
    const projected = projectSessionMessages(raw);
    expect(projected).toHaveLength(1);
    expect(projected[0].content).toEqual([
      { type: "thinking", text: "considering options" },
      { type: "text", text: "here you go" },
    ]);
  });

  it("normalizes both `ts` and `timestamp` onto HistoryMessage.timestamp", () => {
    const raw = [
      { role: "user", content: [{ type: "text", text: "first" }], ts: 1_700_000_000_000 },
      {
        role: "assistant",
        content: [{ type: "text", text: "ok" }],
        timestamp: 1_700_000_001_000,
      },
      { role: "user", content: [{ type: "text", text: "no ts here" }] },
    ];
    const projected = projectSessionMessages(raw);
    expect(projected.map((m) => m.timestamp)).toEqual([
      1_700_000_000_000,
      1_700_000_001_000,
      undefined,
    ]);
  });

  it("filters out hidden messages (e.g. empty user-text-only messages)", () => {
    // shouldHideHistoryMessage drops user messages whose content is entirely
    // empty text — matches the live-chat hide filter. System / tool roles
    // are NOT dropped here; they're filtered downstream by MessageBlocks
    // (tool_result blocks render inside tool_use pairs).
    const raw = [
      { role: "user", content: [] }, // hidden — empty user
      { role: "assistant", content: [{ type: "text", text: "done" }] },
    ];
    const projected = projectSessionMessages(raw);
    expect(projected).toHaveLength(1);
    expect(projected[0].role).toBe("assistant");
  });
});

describe("adaptSessionMessages", () => {
  it("groups consecutive user/assistant turns into RenderGroups", () => {
    const raw = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "assistant", content: [{ type: "text", text: "hello" }] },
    ];
    const groups = adaptSessionMessages(raw, "session-1");
    expect(groups).toHaveLength(2);

    expect(groups[0].side).toBe("user");
    expect(groups[0].items).toHaveLength(1);
    expect(groups[0].items[0].kind).toBe("message");
    if (groups[0].items[0].kind === "message") {
      expect(groups[0].items[0].message.role).toBe("user");
      expect(groups[0].items[0].message.content).toEqual([
        { type: "text", text: "hi" },
      ]);
    }

    expect(groups[1].side).toBe("assistant");
    expect(groups[1].items).toHaveLength(1);
    expect(groups[1].items[0].kind).toBe("message");
    if (groups[1].items[0].kind === "message") {
      expect(groups[1].items[0].message.role).toBe("assistant");
      expect(groups[1].items[0].message.content).toEqual([
        { type: "text", text: "hello" },
      ]);
    }
  });

  it("emits keys derived from the supplied sessionKey", () => {
    const raw = [
      { role: "assistant", content: [{ type: "text", text: "ok" }] },
    ];
    const groups = adaptSessionMessages(raw, "abc");
    const firstItem = groups[0].items[0];
    expect(firstItem.key).toContain("abc");
  });

  it("hides empty user-text-only messages", () => {
    const raw = [
      { role: "user", content: [] },
      { role: "assistant", content: [{ type: "text", text: "done" }] },
    ];
    const groups = adaptSessionMessages(raw, "s");
    expect(groups).toHaveLength(1);
    expect(groups[0].side).toBe("assistant");
  });
});
