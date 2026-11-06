// @vitest-environment jsdom
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type TestLiveEvent = { type: string; payload: { eventName: string; payload: Record<string, unknown> } };

const live = vi.hoisted(() => ({
  eventHandler: null as ((event: TestLiveEvent) => void) | null,
  chatHandler: null as ((frame: Record<string, unknown>, event: { payload: Record<string, unknown> }) => void) | null,
  notifyFailure: vi.fn(),
  pushSearchParams: vi.fn(),
  notifyLoading: vi.fn(),
  notifySuccess: vi.fn(),
  nativeNotify: vi.fn(),
  dismissNotification: vi.fn(),
  openPanel: vi.fn(),
  notifyAttention: vi.fn(),
  refreshActivity: vi.fn(),
  apiGet: vi.fn(),
}));

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    onEvent: (handler: (event: TestLiveEvent) => void) => {
      live.eventHandler = handler;
      return () => { live.eventHandler = null; };
    },
    onChatMessage: (handler: NonNullable<typeof live.chatHandler>) => {
      live.chatHandler = handler;
      return () => { live.chatHandler = null; };
    },
  }),
}));
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => ({ agents: [] }), agentDisplayName: () => "Agent" }));
vi.mock("@/components/workspace/workspaceController", () => ({
  useWorkspaceState: () => ({ visibleChatAgentIds: [], teamsPanelVisible: false }),
  openPanel: (...args: unknown[]) => live.openPanel(...args),
}));
vi.mock("@/hooks/useActivity", () => ({ useActivity: () => ({ refresh: live.refreshActivity }) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: live.apiGet }) }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: () => ({ data: undefined }) }) }));
vi.mock("@/lib/shellNav", () => ({ pushSearchParams: (params: URLSearchParams) => live.pushSearchParams(params) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/lib/desktopNotify", () => ({ nativeNotify: (...args: unknown[]) => live.nativeNotify(...args) }));
vi.mock("@/lib/notify", () => ({
  notifyLoading: (...args: unknown[]) => live.notifyLoading(...args),
  notifySuccess: (...args: unknown[]) => live.notifySuccess(...args),
  notifyFailure: (...args: unknown[]) => live.notifyFailure(...args),
  notifyAttention: (...args: unknown[]) => live.notifyAttention(...args),
  dismissNotification: (...args: unknown[]) => live.dismissNotification(...args),
}));

import { RunNotifications } from "../RunNotifications";
import { claimRunNotice, setRoomView } from "../roomView";

function emit(eventName: string, payload: Record<string, unknown>) {
  live.eventHandler?.({ type: "paperclip.event", payload: { eventName, payload } });
}

beforeEach(() => {
  live.nativeNotify.mockResolvedValue(false);
  live.refreshActivity.mockResolvedValue([]);
  live.apiGet.mockImplementation(async (path: string) => {
    if (path === "/teams/agents") return { agents: [{ id: "agent-1", name: "Pulse" }] };
    if (path === "/teams/issues/iss-400") return { identifier: "ISOA-400", title: "Draft the launch posts" };
    throw new Error("not found");
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("RunNotifications team run completion", () => {
  it("keeps completion feedback and no working toast", async () => {
    render(<RunNotifications />);
    act(() => emit("teams.heartbeat.run.queued", { runId: "run-42", agentId: "agent-1" }));
    // The toast is the fallback once the native answer resolves; flush it.
    await act(async () => emit("teams.heartbeat.run.status", { runId: "run-42", agentId: "agent-1", status: "succeeded" }));

    expect(live.notifyLoading).not.toHaveBeenCalled();
    expect(live.notifySuccess).toHaveBeenCalledWith(
      "Pulse",
      expect.objectContaining({ dedupeKey: "teams:run-42", description: "Finished a run" }),
    );
  });

  it("uses the native notification instead of the completion toast, clearing any working toast", async () => {
    live.nativeNotify.mockResolvedValue(true);
    render(<RunNotifications />);
    act(() => emit("teams.heartbeat.run.queued", { runId: "run-7", agentId: "agent-1" }));
    await act(async () => emit("teams.heartbeat.run.status", { runId: "run-7", agentId: "agent-1", status: "succeeded", issueId: "iss-400" }));

    // Line 1 who · which task, line 2 what happened; the click opens the run.
    expect(live.nativeNotify).toHaveBeenCalledWith("Pulse · ISOA-400", "Finished: Draft the launch posts", {
      onClick: expect.any(Function), tag: "teams:run-7",
    });
    expect(live.notifySuccess).not.toHaveBeenCalled();
    expect(live.dismissNotification).toHaveBeenCalledWith("teams:run-7");
  });
});

describe("RunNotifications live chat run", () => {
  it("never raises a persistent working toast over the composer", async () => {
    render(<RunNotifications />);
    await act(async () => live.chatHandler?.({ type: "chunk", content: "hi", runId: "run-3" },
      { payload: { runId: "run-3", agentId: "agent-1", roomId: "room-7" } }));
    expect(live.notifyLoading).not.toHaveBeenCalled();
  });

  it("says who, where and the reply's first line when a channel run finishes off screen", async () => {
    const rooms = new Map([["room-gtm", { kind: "channel" as const, name: "GTM Strategy" }]]);
    act(() => setRoomView({ visibleRoomId: null, rooms }));
    render(<RunNotifications />);
    const identity = { payload: { runId: "run-1", agentId: "agent-1", roomId: "room-gtm" } };
    act(() => live.chatHandler?.({ type: "chunk", content: "## Drafted 5 launch posts\nmore", runId: "run-1" }, identity));
    await act(async () => live.chatHandler?.({ type: "run_ended", runId: "run-1", outcome: "done" }, identity));
    expect(live.nativeNotify).toHaveBeenCalledWith("Agent · #GTM Strategy", "Drafted 5 launch posts",
      expect.objectContaining({ tag: "run-1" }));
    act(() => setRoomView({ visibleRoomId: null, rooms: new Map() }));
  });

  it("stays quiet when the reply's own banner already announced this run", async () => {
    const rooms = new Map([["room-gtm", { kind: "channel" as const, name: "GTM Strategy" }]]);
    act(() => setRoomView({ visibleRoomId: null, rooms }));
    expect(claimRunNotice("run-8")).toBe(true); // MessageNotifications got there first
    render(<RunNotifications />);
    const identity = { payload: { runId: "run-8", agentId: "agent-1", roomId: "room-gtm" } };
    act(() => live.chatHandler?.({ type: "chunk", content: "hi", runId: "run-8" }, identity));
    await act(async () => live.chatHandler?.({ type: "run_ended", runId: "run-8", outcome: "done" }, identity));
    expect(live.nativeNotify).not.toHaveBeenCalled();
    expect(live.notifySuccess).not.toHaveBeenCalled();
    act(() => setRoomView({ visibleRoomId: null, rooms: new Map() }));
  });

  it("stays quiet for a run in the room on screen", async () => {
    act(() => setRoomView({ visibleRoomId: "room-gtm", rooms: new Map() }));
    render(<RunNotifications />);
    const identity = { payload: { runId: "run-4", agentId: "agent-1", roomId: "room-gtm" } };
    act(() => live.chatHandler?.({ type: "chunk", content: "hi", runId: "run-4" }, identity));
    await act(async () => live.chatHandler?.({ type: "run_ended", runId: "run-4", outcome: "done" }, identity));
    expect(live.nativeNotify).not.toHaveBeenCalled();
    expect(live.notifySuccess).not.toHaveBeenCalled();
    act(() => setRoomView({ visibleRoomId: null, rooms: new Map() }));
  });
});

describe("RunNotifications in a DM", () => {
  it("lets the reply's own banner speak instead of stacking a second one", async () => {
    const rooms = new Map([["room-dm", { kind: "dm" as const, name: null }]]);
    act(() => setRoomView({ visibleRoomId: null, rooms }));
    render(<RunNotifications />);
    const identity = { payload: { runId: "run-2", agentId: "agent-1", roomId: "room-dm" } };
    act(() => live.chatHandler?.({ type: "chunk", content: "Done", runId: "run-2" }, identity));
    await act(async () => live.chatHandler?.({ type: "run_ended", runId: "run-2", outcome: "done" }, identity));
    expect(live.nativeNotify).not.toHaveBeenCalled();
    expect(live.notifySuccess).not.toHaveBeenCalled();
    act(() => setRoomView({ visibleRoomId: null, rooms: new Map() }));
  });
});

describe("RunNotifications failed team run", () => {
  it("says who, which task and why, and opens the run itself (not the Inbox)", async () => {
    render(<RunNotifications />);
    act(() => emit("teams.heartbeat.run.queued", { runId: "run-5", agentId: "agent-1" }));
    await act(async () => emit("teams.heartbeat.run.status", {
      runId: "run-5", agentId: "agent-1", status: "failed", issueId: "iss-400", errorCode: "process_lost", error: "Process lost",
    }));
    const [title, opts] = live.notifyFailure.mock.calls[0] as [string, { description: string; action: { onClick: () => void } }];
    expect(title).toBe("Pulse · ISOA-400");
    expect(opts.description).toBe("Couldn't finish: the workspace restarted mid-run");
    opts.action.onClick();
    const params = live.pushSearchParams.mock.calls[0][0] as URLSearchParams;
    expect(params.get("panel")).toBe("teams-run-detail");
    expect(params.get("detailId")).toBe("run-5");
  });
});

describe("RunNotifications failed chat run", () => {
  function fail(payload: Record<string, unknown>) {
    live.chatHandler?.({ type: "run_ended", runId: "run-9", outcome: "error" }, { payload });
  }

  it("opens the room the run failed in, not the agent's DM", async () => {
    render(<RunNotifications />);
    await act(async () => fail({ runId: "run-9", agentId: "agent-1", roomId: "room-7" }));
    const opened = vi.fn();
    const selected = vi.fn();
    window.addEventListener("openRoom", opened);
    window.addEventListener("selectAgent", selected);
    const { action } = live.notifyFailure.mock.calls[0][1] as { action: { onClick: () => void } };
    action.onClick();
    window.removeEventListener("openRoom", opened);
    window.removeEventListener("selectAgent", selected);
    expect((opened.mock.calls[0][0] as CustomEvent).detail).toEqual({ roomId: "room-7", threadId: null });
    expect(selected).not.toHaveBeenCalled();
    // Native replaces the toast on desktop, so its click has to land in the same room.
    expect(live.nativeNotify).toHaveBeenCalledWith("Agent", "Couldn't finish: the run hit an error", { onClick: action.onClick, tag: "run-9" });
  });

  it("falls back to the agent's DM when the frame names no room", async () => {
    render(<RunNotifications />);
    await act(async () => fail({ runId: "run-10", agentId: "agent-1" }));
    const selected = vi.fn();
    window.addEventListener("selectAgent", selected);
    (live.notifyFailure.mock.calls[0][1] as { action: { onClick: () => void } }).action.onClick();
    window.removeEventListener("selectAgent", selected);
    expect((selected.mock.calls[0][0] as CustomEvent).detail).toEqual({ agentId: "agent-1", source: "notification" });
  });
});

describe("RunNotifications approvals", () => {
  const approvalItem = {
    id: "paperclip:approval:ap-9", kind: "paperclip", subtype: "approval", roomId: null, messageId: null,
    threadRootMessageId: null, actor: { kind: "agent", id: "pa-1", name: "Growth" }, createdAt: "2026-09-25T01:47:00Z",
    state: "open", resolvedBy: null, resolvedAt: null, read: false, actions: [],
    summary: { title: "Growth: Pilot X auto-replies", requester: { kind: "agent", id: "pa-1", name: "Growth" },
      reason: "Reply to mentions for a week", preview: null, links: [] },
  };

  it("names the agent and the ask, and opens Activity focused on the approval", async () => {
    live.refreshActivity.mockResolvedValue([approvalItem]);
    live.nativeNotify.mockResolvedValue(true);
    render(<RunNotifications />);
    await act(async () => emit("teams.activity.logged", {
      action: "approval.created", entityId: "ap-9", agentId: "pa-1", details: { type: "request_board_approval" },
    }));
    const [title, body, opts] = live.nativeNotify.mock.calls[0] as [string, string, { onClick: () => void }];
    expect(title).toBe("Growth · Approval");
    expect(body).toBe("Needs your approval: Pilot X auto-replies");
    opts.onClick();
    expect(live.openPanel).toHaveBeenCalledWith("activity", { focus: "paperclip:approval:ap-9" }, { pushUrl: true });
  });

  it("falls back to a toast with the generic line when the feed cannot say more", async () => {
    render(<RunNotifications />);
    await act(async () => emit("teams.activity.logged", { action: "approval.created", entityId: "ap-9" }));
    expect(live.notifyAttention).toHaveBeenCalledWith("An agent needs your approval", expect.objectContaining({ dedupeKey: "approval:ap-9" }));
  });
});

describe("RunNotifications agent questions", () => {
  const askItem = {
    id: "paperclip:interaction:iss-4:int-1", kind: "paperclip", subtype: "interaction", roomId: null, messageId: null,
    threadRootMessageId: null, actor: { kind: "agent", id: "pa-1", name: "Growth" }, createdAt: "2026-09-25T01:47:00Z",
    state: "open", resolvedBy: null, resolvedAt: null, read: false, actions: [],
    summary: { title: "Growth needs your confirmation: Launch email", requester: { kind: "agent", id: "pa-1", name: "Growth" },
      reason: "Send it to 2,000 people?", preview: null, links: [] },
  };

  it("names the agent and the task, and opens Activity focused on the question", async () => {
    live.refreshActivity.mockResolvedValue([askItem]);
    live.nativeNotify.mockResolvedValue(true);
    render(<RunNotifications />);
    await act(async () => emit("teams.activity.logged", {
      action: "issue.thread_interaction_created", entityId: "iss-4",
      details: { interactionId: "int-1", interactionKind: "request_confirmation" },
    }));
    const [title, body, opts] = live.nativeNotify.mock.calls[0] as [string, string, { onClick: () => void }];
    expect(title).toBe("Growth");
    expect(body).toBe("Needs your confirmation: Launch email");
    opts.onClick();
    expect(live.openPanel).toHaveBeenCalledWith("activity", { focus: "paperclip:interaction:iss-4:int-1" }, { pushUrl: true });
  });
});
