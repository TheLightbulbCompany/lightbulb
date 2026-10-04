// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";

type Focus = { sessionId: string | null; all?: boolean } | null;
const workspace = vi.hoisted(() => ({
  value: { activeRoomId: null as string | null, sessionFocus: null as Focus, explicitPicks: { current: 0 } },
}));
const activateMessagesPanel = vi.hoisted(() => vi.fn());
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspace: () => workspace.value,
}));
vi.mock("../workspaceController", () => ({ activateMessagesPanel }));

import { ChatHistorySync } from "../ChatHistorySync";

function view(activeRoomId: string | null, sessionFocus: Focus = null, picked = false) {
  if (picked) workspace.value.explicitPicks.current += 1;
  workspace.value = { ...workspace.value, activeRoomId, sessionFocus };
}

afterEach(() => {
  window.history.replaceState(null, "", "/chat");
  workspace.value = { activeRoomId: null, sessionFocus: null, explicitPicks: { current: 0 } };
  vi.clearAllMocks();
});

describe("ChatHistorySync", () => {
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
    view("r1", { sessionId: "s1" });
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
