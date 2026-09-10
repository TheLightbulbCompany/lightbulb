// @vitest-environment jsdom
/**
 * The composer model·effort pill must reflect the ACTIVE session's persisted
 * model/effort (from sessions.list), NOT the hardcoded stubs it used to mount
 * with (selectedModelRef="", thinkingLevel="medium", fastMode=false). Its
 * onChanged must also revalidate sessions.list so a just-persisted patch is
 * re-read on the next render.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import React from "react";

import { AgentChatWindow } from "../AgentChatWindow";
import type { ModelEffortPillProps } from "../ModelEffortPill";
import type { AgentSession } from "@/hooks/useAgentSessions";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn(), isSignedIn: true, userId: "user_1" }),
  useOrganization: () => ({ organization: null, membership: null, isLoaded: true }),
  // AgentChatWindow now reads user for the first-run kickoff hook.
  useUser: () => ({ isSignedIn: true, isLoaded: true, user: { id: "user_1" } }),
}));

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    isConnected: true,
    onChatMessage: () => () => {},
    onEvent: () => () => {},
  }),
}));

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [{ id: "a1", name: "Friday" }] }),
  agentDisplayName: (a: { name?: string } | null) => a?.name ?? "Agent",
}));

vi.mock("../ExecApprovalCards", () => ({ ExecApprovalCards: () => null }));

// Stub the pill so we can assert exactly which props AgentChatWindow wires in.
let pillProps: ModelEffortPillProps | null = null;
vi.mock("../ModelEffortPill", () => ({
  ModelEffortPill: (props: ModelEffortPillProps) => {
    pillProps = props;
    return <div data-testid="pill-stub" />;
  },
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: { provider_choice: "byo_key", byo_provider: "openai" } }),
}));

let sessions: AgentSession[] = [];
const mutateSessions = vi.fn(async () => undefined);
vi.mock("@/hooks/useAgentSessions", () => ({
  useAgentSessions: () => ({ sessions, isLoading: false, mutate: mutateSessions }),
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
  // Lean render branch: skeleton + ChatInput(controls) — no MessageList /
  // UpdateBanner / ExecApprovalCards to stand up, but the pill still mounts.
  isLoadingHistory: true,
  needsBootstrap: false,
  queuedMessages: [],
  removeQueuedMessage: vi.fn(),
  clearQueue: vi.fn(),
  steerMessage: vi.fn(),
  retryQueuedMessage: vi.fn(),
  activeSessionId: null as string | null,
};
let chatState: Record<string, unknown> = {};
vi.mock("@/hooks/useAgentChat", () => ({
  useAgentChat: () => ({ ...baseChat, ...chatState }),
  buildChatSessionKey: (agentId: string, sessionName: string, sessionId: string | null) =>
    sessionId ? `agent:${agentId}:${sessionName}:${sessionId}` : `agent:${agentId}:${sessionName}`,
  BOOTSTRAP_MESSAGE: "bootstrap",
}));

const SID = "11111111-1111-4111-8111-111111111111";

beforeEach(() => {
  pillProps = null;
  mutateSessions.mockClear();
  chatState = {};
  sessions = [];
});

describe("AgentChatWindow — composer pill hydration", () => {
  it("passes the ACTIVE session's persisted model/effort to the pill, not stubs", () => {
    sessions = [
      {
        sessionId: SID,
        key: `agent:a1:user_1:${SID}`,
        label: "Taxes",
        rawLabel: "Taxes",
        model: "openai/gpt-5",
        modelProvider: "openai",
        thinkingLevel: "high",
        fastMode: true,
      },
      // A DIFFERENT session's row must not be the one that hydrates the pill.
      { sessionId: null, key: "agent:a1:user_1", label: "General", rawLabel: "General", model: "bedrock/other", thinkingLevel: "low" },
    ];
    chatState = { activeSessionId: SID };
    render(<AgentChatWindow agentId="a1" />);

    expect(pillProps).not.toBeNull();
    expect(pillProps!.selectedModelRef).toBe("openai/gpt-5");
    expect(pillProps!.thinkingLevel).toBe("high");
    expect(pillProps!.fastMode).toBe(true);
    // Proves we dropped the stubs: the old mount always sent "" / "medium" / false.
    expect(pillProps!.selectedModelRef).not.toBe("");
    expect(pillProps!.thinkingLevel).not.toBe("medium");
  });

  it("resolves a bare model id + separate provider into a full ref", () => {
    sessions = [
      { sessionId: SID, key: `agent:a1:user_1:${SID}`, label: "x", rawLabel: "x", model: "gpt-5", modelProvider: "openai" },
    ];
    chatState = { activeSessionId: SID };
    render(<AgentChatWindow agentId="a1" />);
    expect(pillProps!.selectedModelRef).toBe("openai/gpt-5");
  });

  it("hydrates from the legacy General (null) row when activeSessionId is null", () => {
    sessions = [
      {
        sessionId: null,
        key: "agent:a1:user_1",
        label: "General",
        rawLabel: "General",
        model: "bedrock/claude",
        thinkingLevel: "low",
        fastMode: false,
      },
    ];
    chatState = { activeSessionId: null };
    render(<AgentChatWindow agentId="a1" />);
    expect(pillProps!.selectedModelRef).toBe("bedrock/claude");
    expect(pillProps!.thinkingLevel).toBe("low");
  });

  it("passes empty ref/effort (no stub) when the active row has no overrides", () => {
    sessions = [{ sessionId: SID, key: `agent:a1:user_1:${SID}`, label: "x", rawLabel: "x" }];
    chatState = { activeSessionId: SID };
    render(<AgentChatWindow agentId="a1" />);
    expect(pillProps!.selectedModelRef).toBe("");
    // Not "medium": an unset session inherits the default rather than a stub.
    expect(pillProps!.thinkingLevel).toBe("");
    expect(pillProps!.fastMode).toBe(false);
  });

  it("onChanged revalidates sessions.list so the pill re-reads what was persisted", () => {
    sessions = [{ sessionId: SID, key: `agent:a1:user_1:${SID}`, label: "x", rawLabel: "x", model: "openai/gpt-5" }];
    chatState = { activeSessionId: SID };
    render(<AgentChatWindow agentId="a1" />);
    expect(mutateSessions).not.toHaveBeenCalled();
    pillProps!.onChanged?.();
    expect(mutateSessions).toHaveBeenCalledTimes(1);
  });
});
