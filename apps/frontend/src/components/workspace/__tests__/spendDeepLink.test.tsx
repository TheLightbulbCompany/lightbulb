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
//   1. the reconciler turns the URL into a teams command,
//   2. openPanel adds a real dock panel carrying teamsKey: "spend",
//   3. TeamsPanelRouter MOUNTS a panel for that teamsKey — the absorbed key
//      lands on Money's `section` prop, not the "Unknown panel" fallback.
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
  it("the reconciler derives a teams command for the Spend panel", () => {
    const decision = deriveReconcilerAction("?panel=teams-spend", null);
    expect(decision).not.toBeNull();
    expect(decision!.panelKey).toBe("teams-spend");
    expect(decision!.command?.descriptor).toEqual({
      panelId: "teams-spend",
      component: "teams",
      title: "Spend",
      params: { teamsKey: "spend" },
    });
  });

  it("executing that decision adds a dock panel carrying teamsKey: spend", () => {
    const { api, addPanel } = makeFakeApi();
    attachDockviewApi(api, () => undefined);

    const decision = deriveReconcilerAction("?panel=teams-spend", null)!;
    openPanel(decision.panelKey!, decision.durableParams);

    expect(addPanel).toHaveBeenCalledTimes(1);
    expect(addPanel.mock.calls[0][0]).toMatchObject({
      id: "teams-spend",
      component: "teams",
      params: { teamsKey: "spend" },
    });
    // Re-opening focuses rather than duplicating (the panel id is stable).
    openPanel(decision.panelKey!, decision.durableParams);
    expect(addPanel).toHaveBeenCalledTimes(1);
  });

  it("the latch fires: a second reconcile of the same URL is a no-op", () => {
    const first = deriveReconcilerAction("?panel=teams-spend", null)!;
    expect(deriveReconcilerAction("?panel=teams-spend", first.nextSignature)).toBeNull();
  });

  it("TeamsPanelRouter mounts a panel for that teamsKey — not the Unknown fallback", async () => {
    const decision = deriveReconcilerAction("?panel=teams-spend", null)!;
    const teamsKey = decision.command!.descriptor.params.teamsKey;
    render(<TeamsPanelRouter panel={teamsKey} />);
    expect(await screen.findByTestId("money-panel")).toHaveAttribute("data-section", "spend");
    expect(screen.queryByText(/Unknown panel/)).toBeNull();
  });

  it("the absorbed `payments` key lands on Money's section too", async () => {
    render(<TeamsPanelRouter panel="payments" />);
    expect(await screen.findByTestId("money-panel")).toHaveAttribute("data-section", "payments");
  });

  it("the bare `money` key renders Money with no section", async () => {
    render(<TeamsPanelRouter panel="money" />);
    expect(await screen.findByTestId("money-panel")).toHaveAttribute("data-section", "");
  });

  it("?panel=usage still resolves to the control panel — the old key was not stolen", () => {
    const decision = deriveReconcilerAction("?panel=usage", null)!;
    expect(decision.command?.descriptor).toEqual({
      panelId: "control:usage",
      component: "control",
      title: "Usage",
      params: { controlKey: "usage" },
    });
  });
});
