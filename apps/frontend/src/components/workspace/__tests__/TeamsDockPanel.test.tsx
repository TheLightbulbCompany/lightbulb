// @vitest-environment jsdom
// Pins two TeamsDockPanel host contracts:
//   1. a panel's own param change (a one-shot consumed, a filter set) MERGES
//      into its params through the controller, never replacing them, or a
//      one-shot clear wipes teamsKey and blanks the panel body.
//   2. clearing a durable param (agent-filter chip dismissal) also strips the
//      matching URL param when the current URL command names this panel, so a
//      refresh can't resurrect the dismissed filter.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const routerSpy = vi.fn();
vi.mock("@/components/teams/TeamsPanelRouter", () => ({
  TeamsPanelRouter: (props: Record<string, unknown>) => {
    routerSpy(props);
    return <div data-testid="teams-router" />;
  },
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsWorkspaceStatus: () => ({ kind: "ready" }),
  useTeamsApi: () => ({ read: () => ({ data: undefined }) }),
}));
vi.mock("@/components/teams/TeamsStatusOverlays", () => ({
  ProvisioningOverlay: () => null,
  SubscribeOverlay: () => null,
  ErrorOverlay: () => null,
}));
vi.mock("@/lib/shellNav", () => ({
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));
import { replaceSearchParams } from "@/lib/shellNav";

import { TeamsDockPanel } from "../panels/TeamsDockPanel";
import { _resetControllerForTests, attachShell, openPanel, updatePanelParams, useWorkspaceState } from "../workspaceController";

type Params = Record<string, string | undefined>;

// The place's main view, mounted the way WorkspaceBody mounts it.
function Main() {
  const { main } = useWorkspaceState();
  return main ? <TeamsDockPanel id={main.panelId} params={main.params} /> : null;
}
function show(panelKey: string, params: Record<string, string> = {}, oneShots?: Record<string, string>) {
  openPanel(panelKey, params, { oneShots });
  return render(<Main />);
}

function lastRouterProps(): Record<string, unknown> {
  const call = routerSpy.mock.calls.at(-1);
  if (!call) throw new Error("TeamsPanelRouter never rendered");
  return call[0] as Record<string, unknown>;
}

beforeEach(() => {
  _resetControllerForTests();
  attachShell(() => undefined);
  routerSpy.mockClear();
  vi.mocked(replaceSearchParams).mockClear();
  window.history.replaceState(null, "", "/chat");
});

describe("TeamsDockPanel — partial parameter updates", () => {
  it("a one-shot clear does not wipe teamsKey", () => {
    show("teams-issues", {}, { newIssue: "onboarding" });
    expect(lastRouterProps().panel).toBe("issues");
    expect((lastRouterProps().panelParams as Params).newIssue).toBe("onboarding");

    act(() => (lastRouterProps().onConsumeOneShot as (key: string) => void)("newIssue"));
    expect(lastRouterProps().panel).toBe("issues");
    expect((lastRouterProps().panelParams as Params).newIssue).toBeUndefined();
  });

  it("a durable-param delivery merges into existing params", () => {
    show("teams-issues");
    act(() => updatePanelParams("teams-work", { agent: "a-1" }));
    expect(lastRouterProps().panel).toBe("issues");
    expect((lastRouterProps().panelParams as Params).agent).toBe("a-1");
  });
});

describe("TeamsDockPanel — durable params forwarding", () => {

  it("forwards the agents `view` param — the absorbed org-chart key's only carrier", () => {
    // ?panel=teams-org-chart aliases to teams-agents and its view rides in the
    // params. Dropped here, an old Org chart link would open the roster.
    show("teams-org-chart");
    expect((lastRouterProps().panelParams as Params).view).toBe("chart");
  });

});

describe("TeamsDockPanel — scroll-container wrapper", () => {
  it("marks the wrapper as the panel scroll container (workspace-panel-page)", () => {
    // The wrapper IS the panel's scroll container: `workspace-panel-page`
    // collapses editorial-page's min-height:100vh to the slot
    // (workspace.css) — without it, content below the slot's clip line is
    // unreachable. h-full + overflow-auto complete the scroll model.
    const { container } = show("teams-search");
    const wrapper = container.firstElementChild as HTMLElement;
    for (const cls of ["editorial-page", "workspace-panel-page", "h-full", "overflow-auto"]) {
      expect(wrapper.classList.contains(cls)).toBe(true);
    }
  });

  it("keeps the wrapper a containing block for out-of-flow panel content", () => {
    // jsdom computes no layout, so this pins the CSS declaration itself.
    // Drop `position: relative` here and a panel's absolutely positioned
    // descendants (Tailwind `sr-only` is position:absolute) resolve their
    // containing block past the scrollport, handing an ancestor thousands of
    // px of scrollable overflow and a second scrollbar.
    // `container-type: size` does NOT substitute — its computed `contain` is
    // `none` and it establishes no containing block.
    const css = readFileSync(
      resolve(process.cwd(), "src/components/workspace/workspace.css"),
      "utf8",
    );
    const rule = css.slice(css.indexOf(".workspace-panel-page.editorial-page"));
    expect(rule.slice(0, rule.indexOf("}"))).toMatch(/position:\s*relative/);
  });
});

describe("TeamsDockPanel — durable-param clear strips the URL", () => {
  it("dismissing the agent filter clears ?agent when the URL names this panel", () => {
    window.history.replaceState(null, "", "/chat?panel=teams-issues&agent=a-1");
    show("teams-issues", { agent: "a-1" });

    const onUpdateDurableParam = lastRouterProps().onUpdateDurableParam as (
      key: string,
      value: string | null,
    ) => void;
    act(() => onUpdateDurableParam("agent", null));

    expect(replaceSearchParams).toHaveBeenCalledTimes(1);
    const arg = vi.mocked(replaceSearchParams).mock.calls[0][0] as URLSearchParams;
    expect(arg.toString()).toBe("panel=teams-issues");
    expect((lastRouterProps().panelParams as Params).agent).toBeUndefined();
  });

  it("leaves the URL alone when it names a different panel", () => {
    window.history.replaceState(null, "", "/chat?panel=activity");
    show("teams-issues", { agent: "a-1" });

    const onUpdateDurableParam = lastRouterProps().onUpdateDurableParam as (
      key: string,
      value: string | null,
    ) => void;
    act(() => onUpdateDurableParam("agent", null));

    expect(replaceSearchParams).not.toHaveBeenCalled();
  });
});
