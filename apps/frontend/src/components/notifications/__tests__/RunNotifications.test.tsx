import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import type { ComponentProps } from "react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import { _resetTeamsEventsForTests } from "@/lib/teamsSubscription";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const notifyLoading = vi.fn();
const notifySuccess = vi.fn();
const notifyFailure = vi.fn();
const notifyAttention = vi.fn();
const dismissNotification = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifyLoading: (...a: unknown[]) => notifyLoading(...a),
  notifySuccess: (...a: unknown[]) => notifySuccess(...a),
  notifyFailure: (...a: unknown[]) => notifyFailure(...a),
  notifyAttention: (...a: unknown[]) => notifyAttention(...a),
  dismissNotification: (...a: unknown[]) => dismissNotification(...a),
}));

// The component's handlers swallow their own errors so a notification bug can
// never break chat delivery — which means an unmocked notify export would look
// like a silent pass. pushSearchParams is captured so the toast's View action
// can be asserted on the URL it actually builds.
const pushSearchParams = vi.fn();
vi.mock("@/lib/shellNav", () => ({
  pushSearchParams: (...a: unknown[]) => pushSearchParams(...a),
}));

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [{ id: "agent-bg", name: "Atlas" }] }),
  agentDisplayName: (a: { id: string; name?: string; identity?: { name?: string } }) =>
    a.identity?.name || a.name || a.id,
}));

// Controllable per-test; reset to null (General) in beforeEach.
let mockActiveSessionId: string | null = null;
vi.mock("@/hooks/useActiveSession", () => ({
  useActiveSession: () => ({
    activeSessionId: mockActiveSessionId,
    newSession: vi.fn(),
    selectSession: vi.fn(),
  }),
}));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
let eventHandlers: Array<(event: string, data: unknown) => void> = [];
const send = vi.fn(() => true);

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq: vi.fn(),
    sendChat: vi.fn(),
    onEvent: (h: (event: string, data: unknown) => void) => {
      eventHandlers.push(h);
      return () => {
        eventHandlers = eventHandlers.filter((x) => x !== h);
      };
    },
    onChatMessage: (h: (msg: ChatIncomingMessage) => void) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

import { RunNotifications } from "../RunNotifications";

function dispatchChat(msg: ChatIncomingMessage): void {
  act(() => {
    chatHandlers.forEach((h) => h(msg));
  });
}

function dispatchEvent(event: string, payload: unknown): void {
  act(() => {
    eventHandlers.forEach((h) => h(event, payload));
  });
}

type MobileProps = Extract<ComponentProps<typeof RunNotifications>, { mode: "mobile" }>;

function mount(props?: Partial<Omit<MobileProps, "mode">>) {
  return render(
    <RunNotifications
      mode="mobile"
      selectedAgentId="agent-sel"
      activeView="chat"
      activePanel="overview"
      {...props}
    />,
  );
}

beforeEach(() => {
  chatHandlers = [];
  eventHandlers = [];
  mockActiveSessionId = null;
  _resetTeamsEventsForTests();
  send.mockClear();
  notifyLoading.mockClear();
  notifySuccess.mockClear();
  notifyFailure.mockClear();
  notifyAttention.mockClear();
  dismissNotification.mockClear();
  pushSearchParams.mockClear();
});

describe("RunNotifications — a task paused on human input", () => {
  const pausedEvent = {
    action: "issue.thread_interaction_created",
    entityType: "issue",
    entityId: "issue-1",
    details: { interactionId: "int-1", interactionKind: "request_confirmation" },
  };

  it("raises a sticky toast naming what is being asked", () => {
    mount();
    dispatchEvent("teams.activity.logged", pausedEvent);
    expect(notifyAttention).toHaveBeenCalledTimes(1);
    const [msg, opts] = notifyAttention.mock.calls[0] as [string, { dedupeKey: string }];
    expect(msg).toBe("A task is paused waiting for your confirmation");
    expect(opts.dedupeKey).toBe("interaction:int-1");
  });

  it("routes View to the blocked task, not a generic list", () => {
    mount();
    dispatchEvent("teams.activity.logged", pausedEvent);
    const [, opts] = notifyAttention.mock.calls[0] as [
      string,
      { action: { onClick: () => void } },
    ];
    act(() => opts.action.onClick());
    const params = pushSearchParams.mock.calls[0][0] as URLSearchParams;
    expect(params.get("panel")).toBe("teams-issue-detail");
    expect(params.get("detailId")).toBe("issue-1");
  });

  it("clears the toast once the interaction is answered", () => {
    mount();
    dispatchEvent("teams.activity.logged", pausedEvent);
    dispatchEvent("teams.activity.logged", {
      action: "issue.thread_interaction_accepted",
      entityId: "issue-1",
      details: { interactionId: "int-1" },
    });
    expect(dismissNotification).toHaveBeenCalledWith("interaction:int-1");
  });
});

describe("RunNotifications", () => {
  it("acquires the teams subscription on mount and releases on unmount", () => {
    const { unmount } = mount();
    expect(send).toHaveBeenCalledWith({ type: "teams.subscribe" });
    unmount();
    expect(send).toHaveBeenCalledWith({ type: "teams.unsubscribe" });
  });

  it("shows a named working toast for a background chat run", () => {
    mount();
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-bg" });
    expect(notifyLoading).toHaveBeenCalledTimes(1);
    const [msg, opts] = notifyLoading.mock.calls[0] as [string, { dedupeKey: string }];
    expect(msg).toContain("Atlas");
    expect(opts.dedupeKey).toBe("r1");
  });

  it("suppresses the run the user is watching", () => {
    mount();
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-sel" });
    expect(notifyLoading).not.toHaveBeenCalled();
  });

  it("toasts a background run on a different session of the selected agent", () => {
    mockActiveSessionId = null; // watching General on agent-sel
    mount();
    dispatchChat({
      type: "chunk",
      content: "…",
      runId: "r1",
      agent_id: "agent-sel",
      session_id: "s2",
    });
    expect(notifyLoading).toHaveBeenCalledTimes(1);
  });

  it("resolves a shown run to success with a View action on run_ended done", () => {
    mount();
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-bg" });
    dispatchChat({ type: "run_ended", runId: "r1", outcome: "done", agent_id: "agent-bg" });
    expect(notifySuccess).toHaveBeenCalledTimes(1);
    const [, opts] = notifySuccess.mock.calls[0] as [
      string,
      { dedupeKey: string; action: { label: string; onClick: () => void } },
    ];
    expect(opts.dedupeKey).toBe("r1");
    expect(opts.action.label).toBe("View");
  });

  it("clicking View dispatches a sourced selectAgent event", () => {
    mount();
    const listener = vi.fn();
    window.addEventListener("selectAgent", listener);
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-bg" });
    dispatchChat({ type: "run_ended", runId: "r1", outcome: "done", agent_id: "agent-bg" });
    const [, opts] = notifySuccess.mock.calls[0] as [
      string,
      { action: { onClick: () => void } },
    ];
    act(() => opts.action.onClick());
    const evt = listener.mock.calls[0][0] as CustomEvent<{ agentId: string; source?: string }>;
    expect(evt.detail).toEqual({ agentId: "agent-bg", source: "notification" });
    window.removeEventListener("selectAgent", listener);
  });

  it("dismisses on aborted and marks failures sticky", () => {
    mount();
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-bg" });
    dispatchChat({ type: "run_ended", runId: "r1", outcome: "aborted", agent_id: "agent-bg" });
    expect(dismissNotification).toHaveBeenCalledWith("r1");

    dispatchChat({ type: "chunk", content: "…", runId: "r2", agent_id: "agent-bg" });
    dispatchChat({ type: "run_ended", runId: "r2", outcome: "error", agent_id: "agent-bg" });
    expect(notifyFailure).toHaveBeenCalledTimes(1);
  });

  it("toasts teams heartbeat runs through their lifecycle", () => {
    mount();
    dispatchEvent("teams.heartbeat.run.queued", { runId: "t1", agentId: "pa1" });
    expect(notifyLoading).toHaveBeenCalledTimes(1);
    expect((notifyLoading.mock.calls[0] as [string, { dedupeKey: string }])[1].dedupeKey).toBe("teams:t1");
    dispatchEvent("teams.heartbeat.run.status", { runId: "t1", agentId: "pa1", status: "succeeded" });
    expect(notifySuccess).toHaveBeenCalledTimes(1);
  });

  it("ignores btw_result and non-run teams events", () => {
    mount();
    dispatchChat({ type: "btw_result", question: "q", text: "t" });
    dispatchEvent("teams.activity.logged", { runId: "t1" });
    expect(notifyLoading).not.toHaveBeenCalled();
    expect(notifySuccess).not.toHaveBeenCalled();
  });
});
