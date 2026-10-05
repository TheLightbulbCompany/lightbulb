import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/workspace/workspaceController", () => ({
  getWorkspaceSnapshot: () => ({ activePanelId: "messages" }),
  activateMessagesPanel: vi.fn(),
  openPanel: vi.fn(),
}));

import { pickTokenUnchanged } from "../MessagingWorkspaceContext";

describe("pickTokenUnchanged", () => {
  const a = { href: "http://x/chat", activePanelId: "messages" };
  it("bumps when nothing moved", () => expect(pickTokenUnchanged(a, { ...a })).toBe(true));
  it("does not bump when the active dock tab changed", () =>
    expect(pickTokenUnchanged(a, { ...a, activePanelId: "teams-issues" })).toBe(false));
  it("does not bump when the URL changed", () =>
    expect(pickTokenUnchanged(a, { ...a, href: "http://x/chat?p=1" })).toBe(false));
});
