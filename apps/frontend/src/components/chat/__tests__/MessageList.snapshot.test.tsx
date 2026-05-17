// apps/frontend/src/components/chat/__tests__/MessageList.snapshot.test.tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

describe("MessageList rendering snapshots", () => {
  it("renders an empty list", () => {
    const { container } = render(
      <MessageList renderGroups={[]} isStreaming={false} isTyping={false} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("renders a single user + assistant exchange", () => {
    const groups: RenderGroup[] = [
      {
        side: "user",
        key: "g-user",
        items: [
          {
            kind: "message",
            key: "u1",
            message: { role: "user", content: [{ type: "text", text: "hi" }] },
          },
        ],
      },
      {
        side: "assistant",
        key: "g-asst",
        items: [
          {
            kind: "message",
            key: "a1",
            message: { role: "assistant", content: [{ type: "text", text: "hello" }] },
          },
        ],
      },
    ];
    const { container } = render(
      <MessageList renderGroups={groups} isStreaming={false} isTyping={false} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("renders an assistant bubble with text + tool + more text", () => {
    const groups: RenderGroup[] = [
      {
        side: "assistant",
        key: "g",
        items: [
          {
            kind: "message",
            key: "a",
            message: {
              role: "assistant",
              content: [
                { type: "text", text: "let me check" },
                { type: "tool_use", id: "t1", name: "exec", input: { cmd: "ls" } },
                { type: "tool_result", tool_use_id: "t1", content: "output here" },
                { type: "text", text: "done" },
              ],
            },
          },
        ],
      },
    ];
    const { container } = render(
      <MessageList renderGroups={groups} isStreaming={false} isTyping={false} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("renders live stream + live thinking at the tail", () => {
    const groups: RenderGroup[] = [
      {
        side: "assistant",
        key: "g",
        items: [
          { kind: "live-thinking", key: "t", text: "thinking…", runId: "r1" },
          { kind: "stream", key: "s", text: "the answer is", runId: "r1" },
        ],
      },
    ];
    const { container } = render(
      <MessageList renderGroups={groups} isStreaming={true} isTyping={true} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("renders assistant text from the legacy top-level `message.text` field (Codex P2 on PR #682)", () => {
    // chat.history rows and cron run transcripts can carry assistant
    // text on the top-level `text` field instead of `content[]`. Before
    // the fix, MessageBlocks emitted an empty bubble for these.
    const groups: RenderGroup[] = [
      {
        side: "assistant",
        key: "g",
        items: [
          {
            kind: "message",
            key: "a-legacy",
            message: { role: "assistant", text: "legacy assistant reply" },
          },
        ],
      },
    ];
    render(<MessageList renderGroups={groups} isStreaming={false} isTyping={false} />);
    expect(screen.getByText("legacy assistant reply")).toBeInTheDocument();
  });
});
