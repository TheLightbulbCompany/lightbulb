// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";

type Focus = { sessionId: string | null; all?: boolean } | null;
const workspace = vi.hoisted(() => ({
  value: { activeRoomId: null as string | null, sessionFocus: null as Focus, explicitPickCount: 0 },
}));
const activateMessagesPanel = vi.hoisted(() => vi.fn());
const leavePaneFor = vi.hoisted(() => vi.fn());
const shell = vi.hoisted(() => ({ place: "chat" }));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspace: () => workspace.value,
}));
vi.mock("../workspaceController", () => ({ activateMessagesPanel, leavePaneFor, useWorkspaceState: () => ({ place: shell.place }) }));

import { ChatHistorySync } from "../ChatHistorySync";
import { recordMessagesNavigation } from "../chatHistory";
import { pushSearchParams } from "@/lib/shellNav";

function view(activeRoomId: string | null, sessionFocus: Focus = null, picked = false) {
  workspace.value = { activeRoomId, sessionFocus, explicitPickCount: workspace.value.explicitPickCount + (picked ? 1 : 0) };
}

afterEach(() => {
  window.history.replaceState(null, "", "/chat");
  workspace.value = { activeRoomId: null, sessionFocus: null, explicitPickCount: 0 };
  shell.place = "chat";
  vi.clearAllMocks();
});

describe("ChatHistorySync", () => {
  it("W17-9: another room, or Chat from another place, asks the pane to leave; the first room on load does not", () => {
    const view2 = render(<ChatHistorySync />);
    view("room-a");
    view2.rerender(<ChatHistorySync />); // the room resolves on load
    expect(leavePaneFor).not.toHaveBeenCalled();
    shell.place = "library"; // Creations, a doc opened there
    view2.rerender(<ChatHistorySync />);
    shell.place = "chat"; // back to the same room in Chat
    view2.rerender(<ChatHistorySync />);
    expect(leavePaneFor).toHaveBeenLastCalledWith("room-a");
    view("room-b", null, true);
    view2.rerender(<ChatHistorySync />);
    expect(leavePaneFor).toHaveBeenLastCalledWith("room-b");
    expect(leavePaneFor).toHaveBeenCalledTimes(2);
  });

  it("leaves the cold load's automatic picks alone", () => {
    const shell = render(<ChatHistorySync />);
    view("vendor");
    shell.rerender(<ChatHistorySync />);
    view("general");
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("");
    shell.unmount();
  });

  it("pushes a person's pick", () => {
    view("r1");
    const shell = render(<ChatHistorySync />);
    view("r2", null, true);
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=chat&room=r2");
    shell.unmount();
  });

  it("records an opened session but not the new-session composer", () => {
    view("r1");
    const shell = render(<ChatHistorySync />);
    view("r1", { sessionId: "s1" }, true);
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=chat&session=s1");
    view("r1", { sessionId: null });
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=chat&session=s1");
    shell.unmount();
  });

  it("records a room change from a session view once the session closes", () => {
    window.history.replaceState(null, "", "/chat?panel=chat&session=s1");
    view("r1", { sessionId: "s1" });
    const shell = render(<ChatHistorySync />);
    const before = window.history.length;
    view("r2", { sessionId: "s1" }, true); // the pick lands before the provider clears the session
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=chat&session=s1");
    view("r2", null);
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=chat&room=r2");
    expect(window.history.length).toBe(before + 1);
    shell.unmount();
  });

  it("keeps the rail's Messages button off the last room while the composer is open", () => {
    view("r1");
    const shell = render(<ChatHistorySync />);
    window.history.pushState(null, "", "/chat?panel=teams-issues");
    view("r1", { sessionId: null });
    shell.rerender(<ChatHistorySync />);
    recordMessagesNavigation();
    expect(window.location.search).toBe("?panel=teams-issues");
    shell.unmount();
  });

  it("records re-picking the open room from a panel URL", () => {
    view("r1");
    const shell = render(<ChatHistorySync />);
    window.history.pushState(null, "", "/chat?panel=teams-issues");
    view("r1", null, true);
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=chat&room=r1");
    shell.unmount();
  });

  it("leaves no pending pick behind a pick that changed nothing", () => {
    window.history.replaceState(null, "", "/chat?panel=chat&room=r1");
    view("r1");
    const shell = render(<ChatHistorySync />);
    view("r1", null, true);
    shell.rerender(<ChatHistorySync />);
    window.history.pushState(null, "", "/chat?panel=teams-issues");
    view("r2");
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=teams-issues");
    shell.unmount();
  });

  it("does not record a held session link that lands after the person moved on", () => {
    view("r1");
    const shell = render(<ChatHistorySync />);
    window.history.pushState(null, "", "/chat?panel=teams-issues");
    view("r1", { sessionId: "s1" });
    shell.rerender(<ChatHistorySync />);
    expect(window.location.search).toBe("?panel=teams-issues");
    shell.unmount();
  });

  it("names a bare /chat entry before a panel push leaves it", () => {
    view("r1");
    const shell = render(<ChatHistorySync />);
    const replace = vi.spyOn(window.history, "replaceState");
    pushSearchParams(new URLSearchParams("panel=teams-issues"));
    expect(replace).toHaveBeenCalledExactlyOnceWith(null, "", "/chat?panel=chat&room=r1");
    expect(window.location.search).toBe("?panel=teams-issues");
    replace.mockRestore();
    shell.unmount();
  });

  it("shows Messages when Back lands on a bare URL", () => {
    const shell = render(<ChatHistorySync />);
    act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
    expect(activateMessagesPanel).toHaveBeenCalledTimes(1);
    window.history.replaceState(null, "", "/chat?panel=teams-issues");
    act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
    expect(activateMessagesPanel).toHaveBeenCalledTimes(1);
    shell.unmount();
  });

  it("forgets the view on unmount, so another workspace starts clean", () => {
    view("r1");
    render(<ChatHistorySync />).unmount();
    view("other-ws-room", null, true);
    const next = render(<ChatHistorySync />);
    expect(window.location.search).toBe("");
    next.unmount();
  });
});
