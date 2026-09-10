import { describe, it, expect, vi } from "vitest";
import type { DockviewApi } from "dockview-react";
import { buildMissionControlLayout } from "../buildDefaultLayout";
import { DEFAULT_AGENT_SENTINEL, MESSAGES_PANEL_ID } from "../panelRegistry";

describe("buildMissionControlLayout", () => {
  it("opens chat, browser and inbox as ONE tab group — chat active last", () => {
    const calls: Array<Record<string, unknown>> = [];
    const api = {
      addPanel: vi.fn((opts: Record<string, unknown>) => {
        calls.push(opts);
        return { id: opts.id, api: { setActive: vi.fn() } };
      }),
      getPanel: vi.fn((id: string) => {
        const found = calls.find((c) => c.id === id);
        return found ? { id, api: { setActive: vi.fn() } } : undefined;
      }),
    } as unknown as DockviewApi;

    buildMissionControlLayout(api);

    expect(calls.map((c) => c.id)).toEqual([MESSAGES_PANEL_ID, "browser", "teams-inbox"]);
    expect(calls[0]).toMatchObject({ component: "chat", renderer: "always", params: { agentId: DEFAULT_AGENT_SENTINEL } });
    // Browser id/component must match describePanel's singleton so a later
    // ?panel=browser (or the tool auto-open) focuses this panel, not a twin.
    expect(calls[1]).toMatchObject({ component: "browser", params: {} });
    expect(calls[2]).toMatchObject({ component: "teams" });

    // The shell is tabs-only: NO panel may carry a `position`, because a
    // position is what splits the dock. Without a position dockview falls back
    // to the active group, so all three land as tabs in one group.
    for (const call of calls) {
      expect(call).not.toHaveProperty("position");
    }

    // Only chat renders while hidden. Browser and Inbox must stay on
    // dockview's default `onlyWhenVisible` renderer — Browser's mount effect
    // starts the container's headful Chrome, and an eagerly-rendered tab would
    // boot one for every owner on first load.
    expect(calls[1]).not.toHaveProperty("renderer");
    expect(calls[2]).not.toHaveProperty("renderer");

    // Chat ends focused (spec: default layout focuses the conversation)
    expect(api.getPanel).toHaveBeenCalledWith(MESSAGES_PANEL_ID);
  });
});
