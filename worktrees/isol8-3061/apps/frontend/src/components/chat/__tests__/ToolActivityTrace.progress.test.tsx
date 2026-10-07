/**
 * Trace-row live-progress rendering (C-track).
 *
 * A running tool (tool_use block with no matching tool_result yet) that carries
 * a live `progressText` must surface that output in the row's expansion so the
 * user sees streaming command output while the tool is still executing.
 */

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

function groupWithRunningToolProgress(): RenderGroup[] {
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
                type: "tool_use",
                id: "t1",
                name: "bash",
                args: { cmd: "make" },
                progressText: "compiling...\n",
              },
            ],
          },
        },
      ],
    },
  ];
}

// A COMPLETED tool (tool_use paired with a tool_result) whose result is empty
// but which carries streamed progressText must fall back to that streamed
// output. Mirrors upstream app-tool-stream's single `entry.output`: the result
// phase only overwrites the streamed output when the result is non-empty, so a
// tool that streamed output then ended with no real result keeps showing it.
function groupWithCompletedEmptyResultProgress(): RenderGroup[] {
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
                type: "tool_use",
                id: "t1",
                name: "bash",
                args: { cmd: "make" },
                progressText: "streamed line\n",
              },
              // Terminal result with no payload — the tool is done but produced
              // no output of its own.
              {
                type: "tool_result",
                tool_use_id: "t1",
                content: "",
              },
            ],
          },
        },
      ],
    },
  ];
}

describe("ToolActivityTrace — live progress (C)", () => {
  it("shows progressText output while the tool is still running", () => {
    render(
      <MessageList
        renderGroups={groupWithRunningToolProgress()}
        isStreaming
        agentName="Friday"
      />,
    );
    // Expand the row, then assert the live output is rendered. Addressed by
    // the plain-language label — the raw name "bash" is never on screen.
    fireEvent.click(screen.getByRole("button", { name: /Running make/i }));
    expect(screen.getByText(/compiling\.\.\./)).toBeInTheDocument();
  });

  it("falls back to streamed output for a completed tool with an empty result", () => {
    render(
      <MessageList
        renderGroups={groupWithCompletedEmptyResultProgress()}
        isStreaming={false}
        agentName="Friday"
      />,
    );
    // Expand the row, then assert the streamed output survives into history.
    fireEvent.click(screen.getByRole("button", { name: /Ran make/i }));
    expect(screen.getByText(/streamed line/)).toBeInTheDocument();
  });
});
