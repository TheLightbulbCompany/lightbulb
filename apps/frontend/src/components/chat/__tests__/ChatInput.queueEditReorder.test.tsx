// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

import { ChatInput } from "../ChatInput";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: (): { commands: never[]; isLoading: boolean } => ({
    commands: [],
    isLoading: false,
  }),
}));

describe("ChatInput — editable queue: edit", () => {
  it("commits an edited queued message on Enter", () => {
    const onEditQueued = vi.fn();
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "first queued" }]}
        onEditQueued={onEditQueued}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /edit queued message/i }));
    const editInput = screen.getByRole("textbox", { name: /edit queued message/i });
    expect(editInput).toHaveValue("first queued");

    fireEvent.change(editInput, { target: { value: "first queued, edited" } });
    fireEvent.keyDown(editInput, { key: "Enter" });

    expect(onEditQueued).toHaveBeenCalledWith("q1", "first queued, edited");
    // Back to the normal row afterward.
    expect(screen.queryByRole("textbox", { name: /edit queued message/i })).not.toBeInTheDocument();
  });

  it("commits via the Save button", () => {
    const onEditQueued = vi.fn();
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "first queued" }]}
        onEditQueued={onEditQueued}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /edit queued message/i }));
    const editInput = screen.getByRole("textbox", { name: /edit queued message/i });
    fireEvent.change(editInput, { target: { value: "saved via button" } });
    fireEvent.click(screen.getByRole("button", { name: /save queued message/i }));

    expect(onEditQueued).toHaveBeenCalledWith("q1", "saved via button");
  });

  it("cancels on Escape without committing", () => {
    const onEditQueued = vi.fn();
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "first queued" }]}
        onEditQueued={onEditQueued}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /edit queued message/i }));
    const editInput = screen.getByRole("textbox", { name: /edit queued message/i });
    fireEvent.change(editInput, { target: { value: "typed but abandoned" } });
    fireEvent.keyDown(editInput, { key: "Escape" });

    expect(onEditQueued).not.toHaveBeenCalled();
    expect(screen.getByText("first queued")).toBeInTheDocument();
  });

  it("cancels via the Cancel button", () => {
    const onEditQueued = vi.fn();
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "first queued" }]}
        onEditQueued={onEditQueued}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /edit queued message/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel edit/i }));

    expect(onEditQueued).not.toHaveBeenCalled();
    expect(screen.getByText("first queued")).toBeInTheDocument();
  });

  it("does not render an Edit action when onEditQueued is not provided", () => {
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "first queued" }]}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /edit queued message/i }),
    ).not.toBeInTheDocument();
  });

  it("hides Retry/Steer/Remove/reorder on the row being edited, leaves other rows alone", () => {
    const onRemoveQueued = vi.fn();
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[
          { id: "q1", text: "first queued" },
          { id: "q2", text: "second queued" },
        ]}
        onEditQueued={vi.fn()}
        onRemoveQueued={onRemoveQueued}
        onMoveQueued={vi.fn()}
      />,
    );

    const editButtons = screen.getAllByRole("button", { name: /edit queued message/i });
    fireEvent.click(editButtons[0]!);

    // Only one Remove button left — the second row's.
    expect(screen.getAllByRole("button", { name: /remove queued message/i })).toHaveLength(1);
    // Only one Edit button left — the second row's (the first is mid-edit).
    expect(screen.getAllByRole("button", { name: /edit queued message/i })).toHaveLength(1);
  });
});

describe("ChatInput — editable queue: reorder", () => {
  it("wires Move up/down to onMoveQueued with the item's id and direction", () => {
    const onMoveQueued = vi.fn();
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[
          { id: "q1", text: "first queued" },
          { id: "q2", text: "second queued" },
        ]}
        onMoveQueued={onMoveQueued}
      />,
    );

    const upButtons = screen.getAllByRole("button", { name: /move queued message up/i });
    const downButtons = screen.getAllByRole("button", { name: /move queued message down/i });
    expect(upButtons).toHaveLength(2);
    expect(downButtons).toHaveLength(2);

    fireEvent.click(downButtons[0]!);
    expect(onMoveQueued).toHaveBeenCalledWith("q1", "down");

    fireEvent.click(upButtons[1]!);
    expect(onMoveQueued).toHaveBeenCalledWith("q2", "up");
  });

  it("disables Move up on the first row and Move down on the last row", () => {
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[
          { id: "q1", text: "first queued" },
          { id: "q2", text: "second queued" },
        ]}
        onMoveQueued={vi.fn()}
      />,
    );

    const upButtons = screen.getAllByRole("button", { name: /move queued message up/i });
    const downButtons = screen.getAllByRole("button", { name: /move queued message down/i });
    expect(upButtons[0]).toBeDisabled();
    expect(downButtons[0]).not.toBeDisabled();
    expect(upButtons[1]).not.toBeDisabled();
    expect(downButtons[1]).toBeDisabled();
  });

  it("does not render reorder controls for a single-item queue", () => {
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "only one" }]}
        onMoveQueued={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /move queued message/i }),
    ).not.toBeInTheDocument();
  });

  it("does not render reorder controls when onMoveQueued is not provided", () => {
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[
          { id: "q1", text: "first queued" },
          { id: "q2", text: "second queued" },
        ]}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /move queued message/i }),
    ).not.toBeInTheDocument();
  });
});
