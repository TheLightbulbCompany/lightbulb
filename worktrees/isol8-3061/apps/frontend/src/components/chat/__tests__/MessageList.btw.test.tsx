/**
 * T8: `/btw` answers render as a distinct, ephemeral "BTW" side card.
 *
 * The backend forwards a `btw_result` frame for a `/btw …` aside; useAgentChat
 * appends a transient side-result render item (NOT pushed into history/cache,
 * mirroring OpenClaw's "Not saved to chat history"). MessageList renders that
 * item as a bordered card labeled "BTW · not saved" — the question in muted
 * text, the answer as the body, error styling when `isError`.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

function btwGroups(isError = false): RenderGroup[] {
  return [
    {
      side: "btw",
      key: "g-btw",
      items: [
        {
          kind: "btw",
          key: "btw:run-1",
          question: "what time is it",
          text: "It is 3pm.",
          isError,
        },
      ],
    },
  ];
}

describe("MessageList /btw side card", () => {
  it("renders a BTW side card with the answer text", () => {
    render(
      <MessageList
        renderGroups={btwGroups()}
        isStreaming={false}
        agentName="Friday"
      />,
    );
    expect(screen.getByText(/BTW/i)).toBeInTheDocument();
    expect(screen.getByText("It is 3pm.")).toBeInTheDocument();
  });

  it("shows the question in muted text and a 'not saved' affordance", () => {
    render(
      <MessageList
        renderGroups={btwGroups()}
        isStreaming={false}
        agentName="Friday"
      />,
    );
    expect(screen.getByText("what time is it")).toBeInTheDocument();
    expect(screen.getByText(/not saved/i)).toBeInTheDocument();
  });

  it("applies error styling when isError is set", () => {
    render(
      <MessageList
        renderGroups={btwGroups(true)}
        isStreaming={false}
        agentName="Friday"
      />,
    );
    const card = screen.getByTestId("btw-card");
    // Reuses the existing chat error tokens (border/text var(--err), a
    // low-alpha danger-tinted fill — see ToolActivityTrace.tsx for the same
    // color-mix pattern).
    expect(card.className).toContain("color-mix(in_srgb,var(--err)_6%,var(--cream))");
  });

  it("renders without error styling by default", () => {
    render(
      <MessageList
        renderGroups={btwGroups(false)}
        isStreaming={false}
        agentName="Friday"
      />,
    );
    const card = screen.getByTestId("btw-card");
    expect(card.className).not.toContain("color-mix(in_srgb,var(--err)_6%,var(--cream))");
  });
});
