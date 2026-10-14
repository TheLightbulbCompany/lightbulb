// @vitest-environment jsdom
// Pins two TeamsDockPanel host contracts:
//   1. dockview's onDidParametersChange delivers the PARTIAL update object —
//      the host must MERGE it (deleting explicit undefineds), never replace
//      its params state, or a one-shot clear wipes teamsKey and blanks the
//      panel body.
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

type PanelProps = Parameters<typeof TeamsDockPanel>[0];

type Params = Record<string, string | undefined>;

function makePanelProps(id: string, params: Params) {
  let handler: ((next: Params) => void) | undefined;
  const api = {
    id,
    onDidParametersChange: (h: (next: Params) => void) => {
      handler = h;
      return { dispose: vi.fn() };
    },
    // Mimic dockview-core: updateParameters fires the RAW partial argument.
    updateParameters: vi.fn((p: Params) => handler?.(p)),
  };
  const props = { api, containerApi: {}, params } as unknown as PanelProps;
  return { props, api, fire: (p: Params) => handler?.(p) };
}

function lastRouterProps(): Record<string, unknown> {
  const call = routerSpy.mock.calls.at(-1);
  if (!call) throw new Error("TeamsPanelRouter never rendered");
  return call[0] as Record<string, unknown>;
}

beforeEach(() => {
  routerSpy.mockClear();
  vi.mocked(replaceSearchParams).mockClear();
  window.history.replaceState(null, "", "/chat");
});

describe("TeamsDockPanel — partial parameter updates", () => {
  it("a one-shot clear ({newIssue: undefined}) does not wipe teamsKey", () => {
    const { props, fire } = makePanelProps("teams-inbox", {
      teamsKey: "inbox",
      newIssue: "onboarding",
    });
    render(<TeamsDockPanel {...props} />);
    expect(lastRouterProps().panel).toBe("inbox");
    expect((lastRouterProps().panelParams as Params).newIssue).toBe("onboarding");

    act(() => fire({ newIssue: undefined }));

    // The router must still receive the panel key — a replaced (not merged)
    // params state would render "Unknown panel: undefined" here.
    expect(lastRouterProps().panel).toBe("inbox");
    expect((lastRouterProps().panelParams as Params).newIssue).toBeUndefined();
  });

  it("a durable-param delivery merges into existing params", () => {
    const { props, fire } = makePanelProps("teams-issues", { teamsKey: "issues" });
    render(<TeamsDockPanel {...props} />);
    act(() => fire({ agent: "a-1" }));
    expect(lastRouterProps().panel).toBe("issues");
    expect((lastRouterProps().panelParams as Params).agent).toBe("a-1");
  });
});

describe("TeamsDockPanel — durable params forwarding", () => {
  // The dock panel's own params-forwarding spread once silently dropped a
  // durable param before it ever reached the router; these are the wire.
  it("forwards the money `day` param through to TeamsPanelRouter", () => {
    const { props } = makePanelProps("teams-money", { teamsKey: "money", day: "2026-09-05" });
    render(<TeamsDockPanel {...props} />);
    expect((lastRouterProps().panelParams as Params).day).toBe("2026-09-05");
  });

  it("forwards the agents `view` param — the absorbed org-chart key's only carrier", () => {
    // ?panel=teams-org-chart aliases to teams-agents and its view rides in the
    // params. Dropped here, an old Org chart link would open the roster.
    const { props } = makePanelProps("teams-agents", { teamsKey: "agents", view: "chart" });
    render(<TeamsDockPanel {...props} />);
    expect((lastRouterProps().panelParams as Params).view).toBe("chart");
  });

  it("forwards the money `section` param — the absorbed keys' only carrier", () => {
    // ?panel=teams-spend aliases to teams-money and its section rides in the
    // params. Dropped here (the same spread that once dropped `path`), every
    // absorbed key would open Money at the top of the page instead.
    const { props } = makePanelProps("teams-money", { teamsKey: "money", section: "spend" });
    render(<TeamsDockPanel {...props} />);
    expect((lastRouterProps().panelParams as Params).section).toBe("spend");
  });
});

describe("TeamsDockPanel — scroll-container wrapper", () => {
  it("marks the wrapper as the panel scroll container (workspace-panel-page)", () => {
    // The wrapper IS the panel's scroll container: `workspace-panel-page`
    // collapses editorial-page's min-height:100vh to the dock slot
    // (workspace.css) — without it, content below the slot's clip line is
    // unreachable. h-full + overflow-auto complete the scroll model.
    const { props } = makePanelProps("teams-dashboard", { teamsKey: "dashboard" });
    const { container } = render(<TeamsDockPanel {...props} />);
    const wrapper = container.firstElementChild as HTMLElement;
    for (const cls of ["editorial-page", "workspace-panel-page", "h-full", "overflow-auto"]) {
      expect(wrapper.classList.contains(cls)).toBe(true);
    }
  });

  it("keeps the wrapper a containing block for out-of-flow panel content", () => {
    // jsdom computes no layout, so this pins the CSS declaration itself.
    // dockview gives `.dv-view` overflow:auto and its `.dv-groupview` child is
    // position:static — drop `position: relative` here and a panel's
    // absolutely positioned descendants (Tailwind `sr-only` is
    // position:absolute) resolve their containing block up to `.dv-view`,
    // handing an 838px slot thousands of px of scrollable overflow. Measured
    // on a long task page: scrollHeight 4640 vs clientHeight 838, a second
    // scrollbar that drags the whole group off-screen.
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
    const { props } = makePanelProps("teams-work", { teamsKey: "issues", agent: "a-1" });
    render(<TeamsDockPanel {...props} />);

    const onUpdateDurableParam = lastRouterProps().onUpdateDurableParam as (
      key: string,
      value: string | null,
    ) => void;
    act(() => onUpdateDurableParam("agent", null));

    expect(replaceSearchParams).toHaveBeenCalledTimes(1);
    const arg = vi.mocked(replaceSearchParams).mock.calls[0][0] as URLSearchParams;
    expect(arg.toString()).toBe("panel=teams-issues");
  });

  it("leaves the URL alone when it names a different panel", () => {
    window.history.replaceState(null, "", "/chat?panel=teams-inbox");
    const { props } = makePanelProps("teams-issues", { teamsKey: "issues", agent: "a-1" });
    render(<TeamsDockPanel {...props} />);

    const onUpdateDurableParam = lastRouterProps().onUpdateDurableParam as (
      key: string,
      value: string | null,
    ) => void;
    act(() => onUpdateDurableParam("agent", null));

    expect(replaceSearchParams).not.toHaveBeenCalled();
  });
});
