// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

import { ChatInput } from "../ChatInput";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
import { capture } from "@/lib/analytics";

// ChatInput consumes useSlashCommands (gateway-backed) for the slash menu.
// These tests render without a GatewayProvider, so stub the hook to an empty
// command set — none of these cases exercise the menu.
vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: (): { commands: never[]; isLoading: boolean } => ({
    commands: [],
    isLoading: false,
  }),
}));

describe("ChatInput — analytics ownership", () => {
  it("does NOT capture chat_message_sent on submit — dispatchNow is the single capture point", () => {
    // A click is not a send: parked/queued messages must only count when
    // they actually dispatch, and a second capture here would double-count
    // every direct send.
    render(<ChatInput onSend={vi.fn()} />);
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: /send message/i }));
    expect(vi.mocked(capture)).not.toHaveBeenCalledWith(
      "chat_message_sent",
      expect.anything(),
    );
  });
});

describe("ChatInput — Queue affordance", () => {
  it("labels the send button 'Send' when idle and 'Queue' while streaming", () => {
    const { rerender } = render(<ChatInput onSend={vi.fn()} />);
    // Idle: the action is "Send".
    expect(screen.getByRole("button", { name: /send message/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /queue message/i })).not.toBeInTheDocument();

    // Streaming: the same action button becomes "Queue".
    rerender(<ChatInput onSend={vi.fn()} isStreaming onStop={vi.fn()} />);
    expect(screen.getByRole("button", { name: /queue message/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^send message$/i })).not.toBeInTheDocument();
  });

  it("submitting while streaming routes through onSend (the hook enqueues)", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} isStreaming onStop={vi.fn()} />);
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "queued text" } });
    fireEvent.click(screen.getByRole("button", { name: /queue message/i }));
    expect(onSend).toHaveBeenCalledWith("queued text", undefined);
  });

  it("renders the queued list with each message's text and a remove action", () => {
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
        onRemoveQueued={onRemoveQueued}
      />,
    );
    expect(screen.getByText("first queued")).toBeInTheDocument();
    expect(screen.getByText("second queued")).toBeInTheDocument();

    const removeButtons = screen.getAllByRole("button", {
      name: /remove queued message/i,
    });
    expect(removeButtons).toHaveLength(2);
    fireEvent.click(removeButtons[0]);
    expect(onRemoveQueued).toHaveBeenCalledWith("q1");
  });

  it("does not render the queued list when there are no queued messages", () => {
    render(<ChatInput onSend={vi.fn()} isStreaming onStop={vi.fn()} />);
    expect(
      screen.queryByRole("button", { name: /remove queued message/i }),
    ).not.toBeInTheDocument();
  });
});

describe("ChatInput — Steer action", () => {
  it("shows a Steer action on a plain queued item while streaming", () => {
    const onSteer = vi.fn();
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "steer me" }]}
        onSteerQueued={onSteer}
      />,
    );
    const steerButton = screen.getByRole("button", {
      name: /steer queued message/i,
    });
    expect(steerButton).toBeInTheDocument();
    fireEvent.click(steerButton);
    expect(onSteer).toHaveBeenCalledWith("q1");
  });

  it("does NOT show Steer when not streaming", () => {
    render(
      <ChatInput
        onSend={vi.fn()}
        queuedMessages={[{ id: "q1", text: "steer me" }]}
        onSteerQueued={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /steer queued message/i }),
    ).not.toBeInTheDocument();
  });

  it("does NOT show Steer on a failed item", () => {
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "boom", sendState: "failed" }]}
        onSteerQueued={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /steer queued message/i }),
    ).not.toBeInTheDocument();
  });
});

describe("ChatInput — Retry action", () => {
  it("shows a Retry action on a failed item and wires it", () => {
    const onRetry = vi.fn();
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "boom", sendState: "failed" }]}
        onRetryQueued={onRetry}
      />,
    );
    const retryButton = screen.getByRole("button", {
      name: /retry queued message/i,
    });
    expect(retryButton).toBeInTheDocument();
    fireEvent.click(retryButton);
    expect(onRetry).toHaveBeenCalledWith("q1");
  });

  it("does NOT show Retry on a non-failed item", () => {
    render(
      <ChatInput
        onSend={vi.fn()}
        isStreaming
        onStop={vi.fn()}
        queuedMessages={[{ id: "q1", text: "ok" }]}
        onRetryQueued={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /retry queued message/i }),
    ).not.toBeInTheDocument();
  });
});
