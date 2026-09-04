// @vitest-environment jsdom
/**
 * `trial_first_agent_response` must mean "the trial owner has now seen agent
 * text" — including Friday's autonomous first-run report, which arrives with
 * nobody having typed. Measured on a live prod signup (2026-09-04): Friday's
 * report landed at +5m29s, but the event was bound to the USER's turn
 * completing and fired at +6m24s. A trial owner who reads the report and
 * never types would never have fired it at all.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import React from "react";

const mockCapture = vi.fn();
vi.mock("@/lib/analytics", () => ({ capture: (...a: unknown[]) => mockCapture(...a) }));

let subscriptionStatus: string | undefined = "trial_local";
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: subscriptionStatus ? { subscription_status: subscriptionStatus } : null }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("t"), isSignedIn: true, userId: "user_1" }),
  useUser: () => ({ isSignedIn: true, isLoaded: true, user: { id: "user_1" } }),
  useOrganization: () => ({ organization: null, membership: null, isLoaded: true }),
}));
vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({ isConnected: true, onChatMessage: () => () => {}, onEvent: () => () => {} }),
}));
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [{ id: "a1", name: "Friday" }] }),
  agentDisplayName: (a: { name?: string } | null) => a?.name ?? "Agent",
}));
vi.mock("../ExecApprovalCards", () => ({ ExecApprovalCards: () => null }));

const baseChat = {
  renderGroups: [] as unknown[],
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
  buildChatSessionKey: (agentId: string, sessionName: string, sessionId: string | null) =>
    sessionId ? `agent:${agentId}:${sessionName}:${sessionId}` : `agent:${agentId}:${sessionName}`,
  BOOTSTRAP_MESSAGE: "bootstrap",
}));

import { AgentChatWindow } from "../AgentChatWindow";

const assistantTextGroup = {
  side: "assistant",
  key: "g1",
  items: [
    {
      kind: "message",
      key: "m1",
      // The persisted shape Friday's first-run report arrives in.
      message: { role: "assistant", content: "Bottled Fire — here is your board." },
    },
  ],
};
const assistantToolOnlyGroup = {
  side: "assistant",
  key: "g2",
  items: [
    {
      kind: "message",
      key: "m2",
      message: { role: "assistant", content: [{ type: "tool_use", id: "t1", name: "read", args: {} }] },
    },
  ],
};
const userGroup = {
  side: "user",
  key: "g3",
  items: [{ kind: "message", key: "m3", message: { role: "user", content: "hi" } }],
};

function fired(): number {
  return mockCapture.mock.calls.filter((c) => c[0] === "trial_first_agent_response").length;
}

describe("trial_first_agent_response", () => {
  beforeEach(() => {
    mockCapture.mockClear();
    window.localStorage.clear();
    subscriptionStatus = "trial_local";
    chatState = {};
  });

  it("fires when the first assistant TEXT is visible — no user turn required", () => {
    chatState = { renderGroups: [assistantTextGroup] };
    render(<AgentChatWindow agentId="a1" />);
    expect(fired()).toBe(1);
  });

  it("does not fire for a user message alone", () => {
    chatState = { renderGroups: [userGroup] };
    render(<AgentChatWindow agentId="a1" />);
    expect(fired()).toBe(0);
  });

  it("does not fire for tool-only assistant activity — the owner has not been answered", () => {
    chatState = { renderGroups: [assistantToolOnlyGroup] };
    render(<AgentChatWindow agentId="a1" />);
    expect(fired()).toBe(0);
  });

  it("fires for a streamed segment too", () => {
    chatState = {
      renderGroups: [
        { side: "assistant", key: "g4", items: [{ kind: "stream-segment", key: "s1", text: "Working on it.", ts: 1 }] },
      ],
    };
    render(<AgentChatWindow agentId="a1" />);
    expect(fired()).toBe(1);
  });

  it("is once-ever per owner across mounts", () => {
    chatState = { renderGroups: [assistantTextGroup] };
    const first = render(<AgentChatWindow agentId="a1" />);
    first.unmount();
    render(<AgentChatWindow agentId="a1" />);
    expect(fired()).toBe(1);
  });

  it("never fires for a paid owner", () => {
    subscriptionStatus = "active";
    chatState = { renderGroups: [assistantTextGroup] };
    render(<AgentChatWindow agentId="a1" />);
    expect(fired()).toBe(0);
  });
});
