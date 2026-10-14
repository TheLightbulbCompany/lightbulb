// @vitest-environment jsdom
//
// The `?panel=teams-spend` deep link, proved along the REAL desktop path
// rather than at the registry.
//
// Registry tests are not enough: they call describePanel directly, while a
// desktop deep link goes URL → deriveReconcilerAction → openPanel → dockview
// params → TeamsDockPanel → TeamsPanelRouter. Two deep-link defects have
// shipped green because every unit test stopped at describePanel. So this
// walks the whole seam:
//
//   1. the reconciler turns the URL into a teams command for Money,
//   2. openPanel adds ONE dock panel (id teams-money) carrying section: "spend",
//   3. TeamsPanelRouter MOUNTS Money for it — the absorbed key lands on the
//      `section` prop, not the "Unknown panel" fallback.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { DockviewApi } from "dockview-react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));

// Stubbed so the mount assertion is about ROUTING, not MoneyPanel's data hooks.
vi.mock("@/components/teams/panels/MoneyPanel", () => ({
  MoneyPanel: (p: { section?: string }) => <div data-testid="money-panel" data-section={p.section ?? ""} />,
}));

import { deriveReconcilerAction } from "../workspaceShellReconciler";
import { openPanel, attachDockviewApi, _resetControllerForTests } from "../workspaceController";
import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

type AddPanelOpts = {
  id: string;
  component?: string;
  title?: string;
  params?: Record<string, string>;
};

function makeFakeApi() {
  const panels = new Map<string, unknown>();
  const addPanel = vi.fn((opts: AddPanelOpts) => {
    const panel = {
      id: opts.id,
      params: opts.params ?? {},
      title: opts.title,
      api: { setActive: vi.fn(), setTitle: vi.fn(), updateParameters: vi.fn() },
    };
    panels.set(opts.id, panel);
    return panel;
  });
  const api = {
    getPanel: (id: string) => panels.get(id),
    addPanel,
    onDidActivePanelChange: () => ({ dispose: vi.fn() }),
    onDidAddPanel: () => ({ dispose: vi.fn() }),
    onDidRemovePanel: () => ({ dispose: vi.fn() }),
    onDidLayoutChange: () => ({ dispose: vi.fn() }),
    panels: [] as unknown[],
    groups: [] as unknown[],
    activePanel: undefined,
  };
  Object.defineProperty(api, "panels", { get: () => [...panels.values()] });
  return { api: api as unknown as DockviewApi, addPanel };
}

beforeEach(() => {
  _resetControllerForTests();
  vi.clearAllMocks();
});

describe("?panel=teams-spend resolves and mounts through the reconciler seam", () => {
  it("the reconciler derives a teams command for Money, at its Spend section", () => {
    const decision = deriveReconcilerAction("?panel=teams-spend", null);
    expect(decision).not.toBeNull();
    expect(decision!.panelKey).toBe("teams-spend");
    expect(decision!.command?.descriptor).toEqual({
      panelId: "teams-work",
      component: "teams",
      title: "Work",
      params: { teamsKey: "money", section: "spend" },
    });
  });

  it("executing that decision adds ONE Money dock panel carrying section: spend", () => {
    const { api, addPanel } = makeFakeApi();
    attachDockviewApi(api, () => undefined);

    const decision = deriveReconcilerAction("?panel=teams-spend", null)!;
    openPanel(decision.panelKey!, decision.durableParams);

    expect(addPanel).toHaveBeenCalledTimes(1);
    expect(addPanel.mock.calls[0][0]).toMatchObject({
      id: "teams-work",
      component: "teams",
      params: { teamsKey: "money", section: "spend" },
    });
    // Re-opening focuses rather than duplicating (the panel id is stable).
    openPanel(decision.panelKey!, decision.durableParams);
    expect(addPanel).toHaveBeenCalledTimes(1);
    // And so does a DIFFERENT absorbed key — one page, one tab. Home links
    // three of these, so without the shared id it opened three "Money" tabs.
    openPanel("teams-payments", {});
    expect(addPanel).toHaveBeenCalledTimes(1);
  });

  it("the latch fires: a second reconcile of the same URL is a no-op", () => {
    const first = deriveReconcilerAction("?panel=teams-spend", null)!;
    expect(deriveReconcilerAction("?panel=teams-spend", first.nextSignature)).toBeNull();
  });

  it("TeamsPanelRouter mounts Money at the section the descriptor carries — not the Unknown fallback", async () => {
    const { params } = deriveReconcilerAction("?panel=teams-spend", null)!.command!.descriptor;
    render(<TeamsPanelRouter panel={params.teamsKey} panelParams={{ section: params.section }} />);
    expect(await screen.findByTestId("money-panel")).toHaveAttribute("data-section", "spend");
    expect(screen.queryByText(/Unknown panel/)).toBeNull();
  });

  it("the bare `money` key renders Money with no section", async () => {
    render(<TeamsPanelRouter panel="money" />);
    expect(await screen.findByTestId("money-panel")).toHaveAttribute("data-section", "");
  });

  it("a junk ?section= never reaches MoneyPanel — it feeds a CSS selector", async () => {
    render(<TeamsPanelRouter panel="money" panelParams={{ section: "constructor" }} />);
    expect(await screen.findByTestId("money-panel")).toHaveAttribute("data-section", "");
  });

  it("a prototype-chain key falls through to Unknown panel, not Money", () => {
    render(<TeamsPanelRouter panel="constructor" />);
    expect(screen.getByText(/Unknown panel/)).toBeInTheDocument();
    expect(screen.queryByTestId("money-panel")).toBeNull();
  });

  it("?panel=usage opens no dock tab — Usage lives on the Settings page", () => {
    expect(deriveReconcilerAction("?panel=usage", null)!.command).toBeNull();
  });
});
