// @vitest-environment jsdom
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

type TestLiveEvent = { type: string; payload: { eventName: string; payload: Record<string, unknown> } };

const live = vi.hoisted(() => ({
  eventHandler: null as ((event: TestLiveEvent) => void) | null,
  pushSearchParams: vi.fn(),
  notifyLoading: vi.fn(),
  notifySuccess: vi.fn(),
}));

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    onEvent: (handler: (event: TestLiveEvent) => void) => {
      live.eventHandler = handler;
      return () => { live.eventHandler = null; };
    },
    onChatMessage: () => () => {},
  }),
}));
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => ({ agents: [] }), agentDisplayName: () => "Agent" }));
vi.mock("@/components/workspace/workspaceController", () => ({
  useWorkspaceState: () => ({ visibleChatAgentIds: [], teamsPanelVisible: false }),
}));
vi.mock("@/lib/shellNav", () => ({ pushSearchParams: (params: URLSearchParams) => live.pushSearchParams(params) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/lib/desktopNotify", () => ({ nativeNotify: vi.fn() }));
vi.mock("@/lib/notify", () => ({
  notifyLoading: (...args: unknown[]) => live.notifyLoading(...args),
  notifySuccess: (...args: unknown[]) => live.notifySuccess(...args),
  notifyFailure: vi.fn(),
  notifyAttention: vi.fn(),
  dismissNotification: vi.fn(),
}));

import { RunNotifications } from "../RunNotifications";
import { clearTeamRunActivity } from "../teamRunActivity";

function emit(eventName: string, payload: Record<string, unknown>) {
  live.eventHandler?.({ type: "paperclip.event", payload: { eventName, payload } });
}

afterEach(() => {
  act(clearTeamRunActivity);
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
  });

  it("removes the inline status and keeps completion feedback", () => {
    render(<RunNotifications mode="mobile" selectedAgentId={null} activeView="chat" activePanel="chat" />);
    act(() => emit("teams.heartbeat.run.queued", { runId: "run-42", agentId: "agent-1" }));
    act(() => emit("teams.heartbeat.run.status", { runId: "run-42", agentId: "agent-1", status: "succeeded" }));

    expect(screen.queryByRole("button", { name: /Team is working/ })).not.toBeInTheDocument();
    expect(live.notifySuccess).toHaveBeenCalledWith(
      "Team agent run completed",
      expect.objectContaining({ dedupeKey: "teams:run-42" }),
    );
  });
});
