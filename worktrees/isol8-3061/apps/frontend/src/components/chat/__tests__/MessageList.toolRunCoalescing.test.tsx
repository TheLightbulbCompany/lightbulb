/**
 * Consolidation happens at RENDER time, not in the data.
 *
 * The live path emits one HistoryMessage per tool call, and that 1:1 shape is
 * load-bearing: `buildChatItems` interleaves `chatStreamSegments[i]` with
 * `chatToolMessages[i]` BY INDEX ("segments[i] is the text before tool[i]"),
 * and per-message timestamps are what keep React keys stable across progress
 * ticks. An earlier attempt merged the calls into one message instead, which
 * stranded every narration segment after the whole run and remounted the trace
 * on every tick — slamming shut any row the user had expanded.
 *
 * These tests pin the render-level behaviour so the data shape stays free to
 * remain 1:1.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

function toolMessage(id: string, name: string, args: unknown, done = true) {
  return {
    kind: "message" as const,
    key: `tool:${id}`,
    message: {
      role: "assistant",
      content: [
        { type: "tool_use", id, name, args },
        ...(done ? [{ type: "tool_result", tool_use_id: id, content: "ok" }] : []),
      ],
    },
  };
}

function textItem(key: string, text: string) {
  return {
    kind: "message" as const,
    key,
    message: { role: "assistant", content: [{ type: "text", text }] },
  };
}

function renderGroup(items: RenderGroup["items"]): RenderGroup[] {
  return [{ side: "assistant", key: "g1", items }];
}

const props = {
  isStreaming: false,
  agentName: "Friday",
};

describe("MessageList — tool run coalescing", () => {
  it("merges separate consecutive tool messages into ONE trace", async () => {
    render(
      <MessageList
        renderGroups={renderGroup([
          toolMessage("t1", "read", { path: "a.ts" }),
          toolMessage("t2", "read", { path: "b.ts" }),
          toolMessage("t3", "bash", { command: "npm test" }),
        ])}
        {...props}
      />,
    );
    // A multi-step trace folds to one summary line by default; open it to see
    // the rows this test is about.
    await userEvent.click(screen.getByRole("button", { expanded: false }));
    // One list, three rows — not three separate indicators.
    expect(screen.getAllByRole("list")).toHaveLength(1);
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });

  it("keeps narration BETWEEN tool calls in its place", () => {
    // The bug this guards: with the calls merged into one message, this text
    // rendered after the whole run, describing work it preceded.
    render(
      <MessageList
        renderGroups={renderGroup([
          toolMessage("t1", "read", { path: "a.ts" }),
          textItem("say", "Checking the results now."),
          toolMessage("t2", "bash", { command: "npm test" }),
        ])}
        {...props}
      />,
    );
    // Two separate traces, split by the prose.
    expect(screen.getAllByRole("list")).toHaveLength(2);

    const lists = screen.getAllByRole("list");
    const prose = screen.getByText("Checking the results now.");
    // The prose sits after the first trace and before the second, in DOM order.
    expect(lists[0]!.compareDocumentPosition(prose) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(prose.compareDocumentPosition(lists[1]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("does not swallow a message that mixes prose with its tool call", () => {
    // A mixed message keeps its own renderer — MessageBlocks already groups
    // within a message and knows where the prose sits between the calls.
    render(
      <MessageList
        renderGroups={renderGroup([
          {
            kind: "message",
            key: "mixed",
            message: {
              role: "assistant",
              content: [
                { type: "tool_use", id: "t1", name: "read", args: { path: "a.ts" } },
                { type: "tool_result", tool_use_id: "t1", content: "ok" },
                { type: "text", text: "Found it." },
              ],
            },
          },
        ])}
        {...props}
      />,
    );
    expect(screen.getByText("Found it.")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("still renders the raw name nowhere", () => {
    render(
      <MessageList
        renderGroups={renderGroup([
          toolMessage("t1", "mcp__openclaw__web_search", { query: "ecs spot" }),
        ])}
        {...props}
      />,
    );
    expect(screen.queryByText(/mcp__openclaw__/)).toBeNull();
    expect(screen.getByText(/Searched the web/)).toBeInTheDocument();
  });
});
