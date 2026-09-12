/**
 * Streaming reveal, the caret, and settle-gated turn actions.
 *
 * The bug this guards: AgentHead rendered Copy/Regenerate/Share with
 * `opacity-0 group-hover:opacity-100` and no `onClick` on any of them — two
 * defects, not one. The buttons were dead (no handler), and hover never
 * fires on touch, so on every phone the controls were also permanently
 * invisible. Settle-gating (mount only once the turn is no longer the live
 * streaming tail) fixes both: reachable on every input modality, and only
 * the two buttons that are actually wired (Copy, Share) ship at all —
 * Regenerate was dropped rather than shipped as a third dead control.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AgentHead, MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

describe("AgentHead — turn actions gated on settle, not hover", () => {
  it("exposes turn actions when the turn settles, on touch as well as pointer", () => {
    const { rerender } = render(<AgentHead name="Friday" streaming />);
    expect(screen.queryByRole("button", { name: /copy/i })).not.toBeInTheDocument();
    rerender(<AgentHead name="Friday" streaming={false} />);
    const copy = screen.getByRole("button", { name: /copy/i });
    // No hover required: the control must be reachable without a pointer.
    expect(copy).toBeVisible();
    expect(copy).toBeEnabled();
  });

  it("copies the message text", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<AgentHead name="Friday" streaming={false} messageText="hello world" />);
    await userEvent.click(screen.getByRole("button", { name: /copy/i }));
    expect(writeText).toHaveBeenCalledWith("hello world");
  });

  it("never renders a Regenerate control — wire it or drop it, never stub it", () => {
    render(<AgentHead name="Friday" streaming={false} messageText="hello world" />);
    expect(screen.queryByRole("button", { name: /regenerate/i })).not.toBeInTheDocument();
  });
});

function streamGroup(text: string): RenderGroup[] {
  return [
    {
      side: "assistant",
      key: "g1",
      items: [{ kind: "stream", key: "s1", text, runId: "run-1" }],
    },
  ];
}

describe("MessageList — streamed block reveal + caret", () => {
  it("renders the caret at the live growing tail", () => {
    const { container } = render(
      <MessageList
        renderGroups={streamGroup("Setting the fallback to on-demand")}
        isStreaming
        agentName="Friday"
      />,
    );
    expect(container.querySelector(".chat-caret")).not.toBeNull();
    // The live tail has no settled actions row yet.
    expect(screen.queryByRole("button", { name: /copy/i })).not.toBeInTheDocument();
  });

  it("drops the caret once the turn is history, not the live tail", () => {
    const { container } = render(
      <MessageList
        renderGroups={[
          {
            side: "assistant",
            key: "g1",
            items: [
              {
                kind: "message",
                key: "m1",
                message: { role: "assistant", content: "Done." },
              },
            ],
          },
        ]}
        isStreaming={false}
        agentName="Friday"
      />,
    );
    expect(container.querySelector(".chat-caret")).toBeNull();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });
});
