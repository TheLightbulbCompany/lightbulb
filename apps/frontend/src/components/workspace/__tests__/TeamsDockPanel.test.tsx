// @vitest-environment jsdom
// Pins the TeamsDockPanel host: Team (the one teams-* page left) reaches the
// router, and the wrapper is the panel's scroll container.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
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

import { TeamsDockPanel } from "../panels/TeamsDockPanel";
import { _resetControllerForTests, attachShell, openPanel, useWorkspaceState } from "../workspaceController";

// The place's main view, mounted the way WorkspaceBody mounts it.
function Main() {
  const { main } = useWorkspaceState();
  return main ? <TeamsDockPanel id={main.panelId} params={main.params} /> : null;
}
function show(panelKey: string) {
  openPanel(panelKey);
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
  window.history.replaceState(null, "", "/chat");
});

describe("TeamsDockPanel — routing", () => {
  it("hands Team's key to the router", () => {
    show("teams-agents");
    expect(lastRouterProps().panel).toBe("agents");
  });
});

describe("TeamsDockPanel — scroll-container wrapper", () => {
  it("marks the wrapper as the panel scroll container (workspace-panel-page)", () => {
    // The wrapper IS the panel's scroll container: `workspace-panel-page`
    // collapses editorial-page's min-height:100vh to the slot
    // (workspace.css) — without it, content below the slot's clip line is
    // unreachable. h-full + overflow-auto complete the scroll model.
    const { container } = show("teams-agents");
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
