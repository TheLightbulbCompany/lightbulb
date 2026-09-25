import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const openPanel = vi.fn();
vi.mock("../../workspaceController", () => ({ openPanel: (...args: unknown[]) => openPanel(...args) }));
vi.mock("@/hooks/useActivity", () => ({ useActivity: vi.fn() }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { openActivityTarget } from "../ActivityDockPanel";

describe("openActivityTarget", () => {
  const opened = vi.fn();
  const listen = (event: Event) => opened((event as CustomEvent).detail);
  beforeEach(() => {
    openPanel.mockClear();
    opened.mockClear();
    window.addEventListener("openRoom", listen);
  });
  afterEach(() => window.removeEventListener("openRoom", listen));

  it("opens rooms and threads through the Messages openRoom event a notification click uses", () => {
    openActivityTarget({ kind: "room", id: "room-1", roomId: "room-1" });
    openActivityTarget({ kind: "thread", id: "msg-1", roomId: "room-1" });
    expect(opened.mock.calls).toEqual([
      [{ roomId: "room-1", threadId: null }],
      [{ roomId: "room-1", threadId: "msg-1" }],
    ]);
    // openPanel("chat", …) only retargets an agent DM: a room link through it went nowhere.
    expect(openPanel).not.toHaveBeenCalled();
  });

  it("opens Paperclip things in their Teams detail", () => {
    openActivityTarget({ kind: "issue", id: "issue-41", roomId: null });
    openActivityTarget({ kind: "run", id: "run-9", roomId: null });
    openActivityTarget({ kind: "approval", id: "ap-2", roomId: null });
    expect(openPanel.mock.calls).toEqual([
      ["teams-issue-detail", { detailId: "issue-41" }, { pushUrl: true }],
      ["teams-run-detail", { detailId: "run-9" }, { pushUrl: true }],
      ["teams-approval-detail", { detailId: "ap-2" }, { pushUrl: true }],
    ]);
  });

  it("opens a shared session through the same event, carrying the session", () => {
    openActivityTarget({ kind: "session", id: "s-1", roomId: null });
    expect(opened.mock.calls).toEqual([[{ sessionId: "s-1" }]]);
    expect(openPanel).not.toHaveBeenCalled();
  });

  it("does nothing for a thread it cannot place in a room", () => {
    openActivityTarget({ kind: "thread", id: "msg-1", roomId: null });
    expect(opened).not.toHaveBeenCalled();
  });
});
