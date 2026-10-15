import { describe, it, expect, vi } from "vitest";
import type { DockviewApi } from "dockview-react";
import { buildMissionControlLayout } from "../buildDefaultLayout";
import { DEFAULT_AGENT_SENTINEL, MESSAGES_PANEL_ID } from "../panelRegistry";

describe("buildMissionControlLayout", () => {
  it("opens chat and activity as ONE tab group — chat active last", () => {
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

    expect(calls.map((c) => c.id)).toEqual([MESSAGES_PANEL_ID, "activity"]);
    expect(calls[0]).toMatchObject({ component: "chat", title: "Chat", renderer: "always", params: { agentId: DEFAULT_AGENT_SENTINEL } });
    expect(calls[1]).toMatchObject({ component: "activity", title: "Activity" });

    // The shell is tabs-only: NO panel may carry a `position`, because a
    // position is what splits the dock. Without a position dockview falls back
    // to the active group, so both land as tabs in one group.
    for (const call of calls) {
      expect(call).not.toHaveProperty("position");
    }

    // Only chat renders while hidden. Activity stays on dockview's default
    // `onlyWhenVisible` renderer so its reads don't fire for every owner on
    // first load.
    expect(calls[1]).not.toHaveProperty("renderer");

    // Chat ends focused (spec: default layout focuses the conversation)
    expect(api.getPanel).toHaveBeenCalledWith(MESSAGES_PANEL_ID);
  });
});
