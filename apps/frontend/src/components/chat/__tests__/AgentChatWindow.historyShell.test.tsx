// @vitest-environment jsdom
/**
 * The chat box must be visible the moment the container is ready — not
 * after history finishes loading. While chat.history is in flight the
 * window renders the chat SHELL (message-area skeleton + input) instead
 * of a bare full-pane spinner; the input is disabled only for that brief
 * window (sending before the history projection lands would let the
 * history replace clobber the optimistic turn).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { AgentChatWindow } from "../AgentChatWindow";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
    userId: "user_1",
  }),
  useOrganization: () => ({ organization: null, membership: null, isLoaded: true }),
}));

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
    onChatMessage: () => () => {},
    onEvent: () => () => {},
  }),
}));

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [{ id: "a1", name: "Friday" }] }),
  agentDisplayName: (a: { name?: string } | null) => a?.name ?? "Agent",
}));

vi.mock("../ExecApprovalCards", () => ({
  ExecApprovalCards: () => null,
}));

const baseChat = {
  renderGroups: [],
  renderItems: [],
  isStreaming: false,
  error: null,
  sendMessage: vi.fn(),
  cancelMessage: vi.fn(),
  clearMessages: vi.fn(),
  isConnected: true,
  needsBootstrap: false,
  queuedMessages: [],
  removeQueuedMessage: vi.fn(),
  clearQueue: vi.fn(),
  steerMessage: vi.fn(),
  retryQueuedMessage: vi.fn(),
};

let chatState: Record<string, unknown> = {};
vi.mock("@/hooks/useAgentChat", () => ({
  useAgentChat: () => ({ ...baseChat, ...chatState }),
  BOOTSTRAP_MESSAGE: "bootstrap",
}));

describe("AgentChatWindow — history-loading shell", () => {
  it("renders the chat input (disabled) while history is loading, not a bare spinner", () => {
    chatState = { isLoadingHistory: true };
    render(<AgentChatWindow agentId="a1" />);

    const textarea = screen.getByRole("textbox");
    expect(textarea).toBeInTheDocument();
    expect(textarea).toBeDisabled();
  });

  it("enables the input once history finishes loading", () => {
    chatState = { isLoadingHistory: false };
    render(<AgentChatWindow agentId="a1" />);

    expect(screen.getByRole("textbox")).toBeEnabled();
  });
});
