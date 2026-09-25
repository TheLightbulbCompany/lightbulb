// @vitest-environment jsdom
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

function emit(eventName: string, payload: Record<string, unknown>) {
  live.eventHandler?.({ type: "paperclip.event", payload: { eventName, payload } });
}

beforeEach(() => {
  live.nativeNotify.mockResolvedValue(false);
  live.refreshActivity.mockResolvedValue([]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("RunNotifications inline team-run status", () => {
  it("renders in-flow on mobile, is keyboard actionable, and avoids the loading toast", async () => {
    const user = userEvent.setup();
    render(<RunNotifications mode="mobile" selectedAgentId={null} activeView="chat" activePanel="chat" />);

    act(() => emit("teams.heartbeat.run.queued", { runId: "run-42", agentId: "agent-1" }));

    const indicator = screen.getByRole("button", { name: "Team is working. View activity" });
    expect(indicator).toHaveClass("team-run-mobile-indicator");
    expect(live.notifyLoading).not.toHaveBeenCalled();

    await user.tab();
    expect(indicator).toHaveFocus();
    await user.keyboard("{Enter}");
    const params = live.pushSearchParams.mock.calls[0]?.[0] as URLSearchParams;
    expect(params.get("panel")).toBe("teams-inbox");
    expect(params.get("section")).toBe("recently");

    // Resolve the run so it doesn't leak into the next test's activity count.
    act(() => emit("teams.heartbeat.run.status", { runId: "run-42", agentId: "agent-1", status: "succeeded" }));
  });

  it("removes the inline status and keeps completion feedback", async () => {
    render(<RunNotifications mode="mobile" selectedAgentId={null} activeView="chat" activePanel="chat" />);
    act(() => emit("teams.heartbeat.run.queued", { runId: "run-42", agentId: "agent-1" }));
    // The toast is the fallback once the native answer resolves; flush it.
    await act(async () => emit("teams.heartbeat.run.status", { runId: "run-42", agentId: "agent-1", status: "succeeded" }));

    expect(screen.queryByRole("button", { name: /Team is working/ })).not.toBeInTheDocument();
    expect(live.notifySuccess).toHaveBeenCalledWith(
      "Team agent run completed",
      expect.objectContaining({ dedupeKey: "teams:run-42" }),
    );
  });

  it("uses the native notification instead of the completion toast, clearing any working toast", async () => {
    live.nativeNotify.mockResolvedValue(true);
    render(<RunNotifications mode="mobile" selectedAgentId={null} activeView="chat" activePanel="chat" />);
    act(() => emit("teams.heartbeat.run.queued", { runId: "run-7", agentId: "agent-1" }));
    await act(async () => emit("teams.heartbeat.run.status", { runId: "run-7", agentId: "agent-1", status: "succeeded" }));

    // Clickable like the failure banner: it opens where the run happened.
    expect(live.nativeNotify).toHaveBeenCalledWith("Team agent run completed", undefined, {
      onClick: expect.any(Function), tag: "teams:run-7",
    });
    expect(live.notifySuccess).not.toHaveBeenCalled();
    expect(live.dismissNotification).toHaveBeenCalledWith("teams:run-7");
  });
});

describe("RunNotifications failed chat run", () => {
  function fail(payload: Record<string, unknown>) {
    live.chatHandler?.({ type: "run_ended", runId: "run-9", outcome: "error" }, { payload });
  }

  it("opens the room the run failed in, not the agent's DM", async () => {
    render(<RunNotifications mode="desktop" />);
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
    expect(live.nativeNotify).toHaveBeenCalledWith("Agent run failed", undefined, { onClick: action.onClick, tag: "run-9" });
  });

  it("falls back to the agent's DM when the frame names no room", async () => {
    render(<RunNotifications mode="desktop" />);
    await act(async () => fail({ runId: "run-9", agentId: "agent-1" }));
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
    render(<RunNotifications mode="desktop" />);
    await act(async () => emit("teams.activity.logged", {
      action: "approval.created", entityId: "ap-9", agentId: "pa-1", details: { type: "request_board_approval" },
    }));
    const [title, body, opts] = live.nativeNotify.mock.calls[0] as [string, string, { onClick: () => void }];
    expect(title).toBe("Growth needs your approval: Pilot X auto-replies");
    expect(body).toBe("Reply to mentions for a week");
    opts.onClick();
    expect(live.openPanel).toHaveBeenCalledWith("activity", { focus: "paperclip:approval:ap-9" }, { pushUrl: true });
  });

  it("falls back to a toast with the generic line when the feed cannot say more", async () => {
    render(<RunNotifications mode="desktop" />);
    await act(async () => emit("teams.activity.logged", { action: "approval.created", entityId: "ap-9" }));
    expect(live.notifyAttention).toHaveBeenCalledWith("An agent needs your approval", expect.objectContaining({ dedupeKey: "approval:ap-9" }));
  });
});
