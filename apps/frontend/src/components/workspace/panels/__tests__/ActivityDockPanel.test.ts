import { beforeEach, describe, expect, it, vi } from "vitest";

const openPanel = vi.fn();
vi.mock("../../workspaceController", () => ({ openPanel: (...args: unknown[]) => openPanel(...args) }));
vi.mock("@/hooks/useActivity", () => ({ useActivity: vi.fn() }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { openActivityTarget } from "../ActivityDockPanel";

describe("openActivityTarget", () => {
  beforeEach(() => openPanel.mockClear());

  it("opens rooms and threads in Messages, Paperclip things in their Teams detail", () => {
    openActivityTarget({ kind: "room", id: "room-1", roomId: null });
    openActivityTarget({ kind: "thread", id: "msg-1", roomId: "room-1" });
    openActivityTarget({ kind: "issue", id: "issue-41", roomId: null });
    openActivityTarget({ kind: "run", id: "run-9", roomId: null });
    openActivityTarget({ kind: "approval", id: "ap-2", roomId: null });
    expect(openPanel.mock.calls).toEqual([
      ["chat", { room: "room-1" }, { pushUrl: true }],
      ["chat", { room: "room-1", message: "msg-1" }, { pushUrl: true }],
      ["teams-issue-detail", { detailId: "issue-41" }, { pushUrl: true }],
      ["teams-run-detail", { detailId: "run-9" }, { pushUrl: true }],
      ["teams-approval-detail", { detailId: "ap-2" }, { pushUrl: true }],
    ]);
  });

  it("does nothing for a thread it cannot place in a room", () => {
    openActivityTarget({ kind: "thread", id: "msg-1", roomId: null });
    expect(openPanel).not.toHaveBeenCalled();
  });
});
