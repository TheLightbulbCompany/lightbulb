/**
 * "Rewind to here" is offered only on a bubble the SERVER has persisted:
 * `sessions.rewind` takes a transcript entryId, and an optimistic local turn
 * has none. It also must never fire on one click — the cut is destructive, so
 * the affordance reveals an inline confirm first.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageList } from "../MessageList";
import type { HistoryMessage, RenderGroup } from "@/hooks/internal/types";

const PERSISTED: HistoryMessage = {
  role: "user",
  content: "what did we decide?",
  __openclaw: { id: "entry-7" },
};
const OPTIMISTIC: HistoryMessage = {
  role: "user",
  content: [{ type: "text", text: "just typed this" }],
  timestamp: 1,
};

function userGroups(...messages: HistoryMessage[]): RenderGroup[] {
  return messages.map((message, i) => ({
    side: "user" as const,
    key: `g${i}`,
    items: [{ kind: "message" as const, key: `m${i}`, message }],
  }));
}

function renderList(
  messages: HistoryMessage[],
  props: { onRewind?: (entryId: string) => void; rewindDisabled?: boolean } = {},
) {
  return render(
    <MessageList
      renderGroups={userGroups(...messages)}
      isStreaming={false}
      isTyping={false}
      agentName="Friday"
      {...props}
    />,
  );
}

describe("MessageList — rewind affordance", () => {
  it("offers rewind on a persisted user bubble", () => {
    renderList([PERSISTED], { onRewind: vi.fn() });
    expect(screen.getByRole("button", { name: /rewind to here/i })).toBeInTheDocument();
  });

  it("offers nothing on an optimistic bubble that has no entry id yet", () => {
    renderList([OPTIMISTIC], { onRewind: vi.fn() });
    expect(screen.queryByRole("button", { name: /rewind to here/i })).not.toBeInTheDocument();
  });

  it("offers nothing when no onRewind handler is wired", () => {
    renderList([PERSISTED]);
    expect(screen.queryByRole("button", { name: /rewind to here/i })).not.toBeInTheDocument();
  });

  it("disables the affordance while a run is active", () => {
    renderList([PERSISTED], { onRewind: vi.fn(), rewindDisabled: true });
    expect(screen.getByRole("button", { name: /rewind to here/i })).toBeDisabled();
  });

  it("requires an inline confirm before firing, and passes the entry id", async () => {
    const onRewind = vi.fn();
    const user = userEvent.setup();
    renderList([PERSISTED], { onRewind });

    await user.click(screen.getByRole("button", { name: /rewind to here/i }));
    expect(onRewind).not.toHaveBeenCalled();
    expect(screen.getByText(/removes everything after this message/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^rewind$/i }));
    expect(onRewind).toHaveBeenCalledWith("entry-7");
  });

  it("cancel dismisses the confirm without rewinding", async () => {
    const onRewind = vi.fn();
    const user = userEvent.setup();
    renderList([PERSISTED], { onRewind });

    await user.click(screen.getByRole("button", { name: /rewind to here/i }));
    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(onRewind).not.toHaveBeenCalled();
    expect(screen.queryByText(/removes everything after this message/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /rewind to here/i })).toBeInTheDocument();
  });
});
