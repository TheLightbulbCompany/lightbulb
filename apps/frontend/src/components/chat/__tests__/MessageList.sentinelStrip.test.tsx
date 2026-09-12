/**
 * Kickoff-transcript presentation (#1425): tool results restored from
 * chat.history must render as a collapsed ToolActivityTrace row (expansion
 * shows the output), never inline prose — and the internal
 * <<<EXTERNAL_UNTRUSTED_CONTENT…>>> injection-defense sentinels are stripped
 * wherever they'd surface (pill output AND assistant markdown).
 */

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

const WRAPPED_OUTPUT = [
  '<<<EXTERNAL_UNTRUSTED_CONTENT id="deadbeef01234567">>>',
  "Source: Web Search",
  "---",
  "Acme Corp makes anvils.",
  '<<<END_EXTERNAL_UNTRUSTED_CONTENT id="deadbeef01234567">>>',
].join("\n");

function groupWithFoldedToolResult(): RenderGroup[] {
  // The shape projectChatHistoryMessages produces after folding a standalone
  // toolResult-role row onto its assistant message.
  return [
    {
      side: "assistant",
      key: "g1",
      items: [
        {
          kind: "message",
          key: "m1",
          message: {
            role: "assistant",
            content: [
              { type: "toolCall", id: "t1", name: "web_search", arguments: { query: "acme" } },
              {
                type: "tool_result",
                tool_use_id: "t1",
                content: [{ type: "text", text: WRAPPED_OUTPUT }],
              },
            ],
          },
        },
      ],
    },
  ];
}

function groupWithSentinelProse(): RenderGroup[] {
  return [
    {
      side: "assistant",
      key: "g1",
      items: [
        {
          kind: "message",
          key: "m1",
          message: {
            role: "assistant",
            content: [
              {
                type: "text",
                text: `Here is what I found:\n\n${WRAPPED_OUTPUT}`,
              },
            ],
          },
        },
      ],
    },
  ];
}

describe("MessageList — tool results & sentinel stripping (#1425)", () => {
  it("renders a folded history tool result as a pill, not prose", () => {
    render(
      <MessageList
        renderGroups={groupWithFoldedToolResult()}
        isStreaming={false}
        agentName="Friday"
      />,
    );
    // Collapsed by default: the raw output is not on screen.
    expect(screen.queryByText(/Acme Corp makes anvils/)).toBeNull();
    // Expand the row — output appears WITHOUT the sentinels. The row is
    // addressed by its plain-language label; the raw tool name is never
    // rendered, which is the whole point of the trace.
    expect(screen.queryByText("web_search")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Searched the web/i }));
    expect(screen.getByText(/Acme Corp makes anvils/)).toBeInTheDocument();
    expect(screen.queryByText(/EXTERNAL_UNTRUSTED_CONTENT/)).toBeNull();
  });

  it("strips sentinel lines from assistant markdown prose", () => {
    render(
      <MessageList
        renderGroups={groupWithSentinelProse()}
        isStreaming={false}
        agentName="Friday"
      />,
    );
    expect(screen.getByText(/Here is what I found/)).toBeInTheDocument();
    expect(screen.getByText(/Acme Corp makes anvils/)).toBeInTheDocument();
    expect(screen.queryByText(/EXTERNAL_UNTRUSTED_CONTENT/)).toBeNull();
  });
});
