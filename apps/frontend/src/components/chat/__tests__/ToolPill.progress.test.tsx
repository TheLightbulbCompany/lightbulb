/**
 * ToolPill live-progress rendering (C-track).
 *
 * A running tool (tool_use block with no matching tool_result yet) that carries
 * a live `progressText` must surface that output in the pill's expansion so the
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

describe("ToolPill — live progress (C)", () => {
  it("shows progressText output while the tool is still running", () => {
    render(
      <MessageList
        renderGroups={groupWithRunningToolProgress()}
        isStreaming
        isTyping={false}
        agentName="Friday"
        onOpenFile={() => {}}
      />,
    );
    // Expand the pill, then assert the live output is rendered.
    fireEvent.click(screen.getByText("bash"));
    expect(screen.getByText(/compiling\.\.\./)).toBeInTheDocument();
  });
});
