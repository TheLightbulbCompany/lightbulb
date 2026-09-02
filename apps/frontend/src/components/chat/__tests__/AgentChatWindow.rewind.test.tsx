// @vitest-environment jsdom
/**
 * Wiring check for the rewind loop: the transcript's affordance calls the
 * hook's `rewindToMessage`, and the text it returns lands in the composer —
 * that hand-off is the whole point of the feature (edit and resend without
 * retyping), and it is the one part no unit test upstream of here covers.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import React from "react";

import { AgentChatWindow } from "../AgentChatWindow";
import type { MessageListProps } from "../MessageList";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn(), isSignedIn: true, userId: "user_1" }),
  useOrganization: () => ({ organization: null, membership: null, isLoaded: true }),
  useUser: () => ({ isSignedIn: true, isLoaded: true, user: { id: "user_1" } }),
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

vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: () => ({ commands: [], isLoading: false }),
}));

vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ account: null }) }));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue({ updates: [] }), post: vi.fn(), uploadFiles: vi.fn() }),
}));

vi.mock("@/hooks/useAgentSessions", () => ({
  useAgentSessions: () => ({ sessions: [], isLoading: false, mutate: vi.fn() }),
}));

vi.mock("../ExecApprovalCards", () => ({ ExecApprovalCards: () => null }));
vi.mock("../AgentProgressCard", () => ({ AgentProgressCard: () => null }));
vi.mock("../ModelEffortPill", () => ({ ModelEffortPill: () => null }));

// Capture what AgentChatWindow wires into the transcript and the branch bar.
let messageListProps: MessageListProps | null = null;
vi.mock("../MessageList", async (orig) => {
  const actual = await orig<typeof import("../MessageList")>();
  return {
    ...actual,
    MessageList: (props: MessageListProps) => {
      messageListProps = props;
      return <div data-testid="message-list-stub" />;
    },
  };
});

let branchBarProps: {
  sessionKey: string | null;
  branchRefreshToken: number;
  disabled?: boolean;
  onSwitch: (id: string) => Promise<boolean>;
} | null = null;
vi.mock("../SessionBranchBar", () => ({
  SessionBranchBar: (props: {
    sessionKey: string | null;
    branchRefreshToken: number;
    disabled?: boolean;
    onSwitch: (id: string) => Promise<boolean>;
  }) => {
    branchBarProps = props;
    return <div data-testid="branch-bar-stub" />;
  },
}));

const rewindToMessage = vi.fn();
const switchBranch = vi.fn();
let chatState: Record<string, unknown> = {};
const baseChat = {
  // Non-empty so the main (transcript) branch renders, not the empty state.
  renderGroups: [{ side: "user", key: "g", items: [] }],
  renderItems: [],
  chatMessages: [],
  isStreaming: false,
  busy: false,
  sendMessage: vi.fn(),
  cancelMessage: vi.fn(),
  clearMessages: vi.fn(),
  isConnected: true,
  isLoadingHistory: false,
  needsBootstrap: false,
  historyTruncated: false,
  queuedMessages: [],
  removeQueuedMessage: vi.fn(),
  clearQueue: vi.fn(),
  steerMessage: vi.fn(),
  retryQueuedMessage: vi.fn(),
  editQueuedMessage: vi.fn(),
  moveQueuedMessage: vi.fn(),
  activeSessionId: null as string | null,
  newSession: vi.fn(),
  selectSession: vi.fn(),
  hasOlderMessages: false,
  isLoadingOlder: false,
  loadOlderMessages: vi.fn(),
  rewindToMessage,
  switchBranch,
  branchRefreshToken: 3,
};
vi.mock("@/hooks/useAgentChat", () => ({
  useAgentChat: () => ({ ...baseChat, ...chatState }),
  buildChatSessionKey: (agentId: string, sessionName: string, sessionId: string | null) =>
    sessionId ? `agent:${agentId}:${sessionName}:${sessionId}` : `agent:${agentId}:${sessionName}`,
  BOOTSTRAP_MESSAGE: "bootstrap",
}));

beforeEach(() => {
  messageListProps = null;
  branchBarProps = null;
  rewindToMessage.mockReset();
  switchBranch.mockReset();
  chatState = {};
});

describe("AgentChatWindow — rewind + branch wiring", () => {
  it("a rewind puts the returned editorText in the composer", async () => {
    rewindToMessage.mockResolvedValue("the prompt that was cut");
    render(<AgentChatWindow agentId="a1" />);

    await act(async () => {
      await messageListProps!.onRewind!("entry-7");
    });

    expect(rewindToMessage).toHaveBeenCalledWith("entry-7");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
      "the prompt that was cut",
    );
  });

  it("keeps the restored draft when the refetch flips the panel to its loading shell", async () => {
    // The rewind's own history invalidation puts isLoadingHistory true, which
    // swaps the whole panel — and its ChatInput — for the loading shell. The
    // draft has to survive that, or the feature delivers nothing.
    rewindToMessage.mockResolvedValue("survives the shell");
    const { rerender } = render(<AgentChatWindow agentId="a1" />);

    await act(async () => {
      await messageListProps!.onRewind!("entry-7");
    });

    chatState = { isLoadingHistory: true };
    rerender(<AgentChatWindow agentId="a1" />);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("survives the shell");

    chatState = {};
    rerender(<AgentChatWindow agentId="a1" />);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("survives the shell");
  });

  it("keeps the restored draft when the rewind emptied the session", async () => {
    // Rewinding to the very FIRST user message leaves nothing to render, so the
    // panel falls to its empty state — a third ChatInput.
    rewindToMessage.mockResolvedValue("the only message");
    const { rerender } = render(<AgentChatWindow agentId="a1" />);

    await act(async () => {
      await messageListProps!.onRewind!("entry-1");
    });

    chatState = { renderGroups: [] };
    rerender(<AgentChatWindow agentId="a1" />);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("the only message");
  });

  it("a failed rewind leaves the composer alone", async () => {
    rewindToMessage.mockResolvedValue(null);
    render(<AgentChatWindow agentId="a1" />);

    await act(async () => {
      await messageListProps!.onRewind!("entry-7");
    });

    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
  });

  it("disables rewind while a run is active", () => {
    chatState = { busy: true };
    render(<AgentChatWindow agentId="a1" />);
    expect(messageListProps!.rewindDisabled).toBe(true);
  });

  it("wires the branch bar to the active session key, the switch, and the refresh token", () => {
    render(<AgentChatWindow agentId="a1" />);
    expect(branchBarProps).not.toBeNull();
    expect(branchBarProps!.sessionKey).toBe("agent:a1:user_1");
    expect(branchBarProps!.branchRefreshToken).toBe(3);
    expect(branchBarProps!.onSwitch).toBe(switchBranch);
    expect(branchBarProps!.disabled).toBe(false);
  });
});
