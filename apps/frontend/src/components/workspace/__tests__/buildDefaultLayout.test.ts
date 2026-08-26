import { describe, it, expect, vi } from "vitest";
import type { DockviewApi } from "dockview-react";
import { buildMissionControlLayout } from "../buildDefaultLayout";

describe("buildMissionControlLayout", () => {
  it("opens sentinel chat, browser right, inbox below browser — chat active last", () => {
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

    expect(calls.map((c) => c.id)).toEqual(["chat:__default__", "browser", "teams-inbox"]);
    expect(calls[0]).toMatchObject({ component: "chat", renderer: "always" });
    // Browser id/component must match describePanel's singleton so a later
    // ?panel=browser (or the tool auto-open) focuses this panel, not a twin.
    expect(calls[1]).toMatchObject({
      component: "browser",
      params: {},
      position: { referencePanel: "chat:__default__", direction: "right" },
    });
    expect(calls[2]).toMatchObject({
      component: "teams",
      position: { referencePanel: "browser", direction: "below" },
    });
    // Chat ends focused (spec: default layout focuses the conversation)
    expect(calls[0].id).toBe("chat:__default__");
  });
});
