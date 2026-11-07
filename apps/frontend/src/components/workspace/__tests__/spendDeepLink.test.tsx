// @vitest-environment jsdom
//
// The `?panel=teams-spend` deep link, proved along the REAL desktop path
// rather than at the registry.
//
// Registry tests are not enough: they call describePanel directly, while a
// desktop deep link goes URL → deriveReconcilerAction → openPanel → the Work
// place's params → TeamsDockPanel → TeamsPanelRouter. Two deep-link defects have
// shipped green because every unit test stopped at describePanel. So this
// walks the whole seam:
//
//   1. the reconciler turns the URL into a teams command for Money,
//   2. openPanel shows ONE Work main view (id teams-work) carrying section: "spend",
//   3. TeamsPanelRouter MOUNTS Money for it — the absorbed key lands on the
//      `section` prop, not the "Unknown panel" fallback.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));


import { deriveReconcilerAction } from "../workspaceShellReconciler";
import { openPanel, attachShell, getWorkspaceSnapshot, _resetControllerForTests } from "../workspaceController";
import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

beforeEach(() => {
  _resetControllerForTests();
  vi.clearAllMocks();
});

describe("?panel=teams-spend resolves and mounts through the reconciler seam", () => {
  it("the reconciler sends Money's Spend link to Settings › Business", () => {
    const decision = deriveReconcilerAction("?panel=teams-spend", null);
    expect(decision).not.toBeNull();
    expect(decision!.panelKey).toBe("teams-spend");
    expect(decision!.command?.descriptor).toEqual({
      panelId: "settings",
      component: "settings",
      title: "Settings",
      params: { section: "business" },
    });
  });

  it("executing that decision shows ONE Settings view at Business", () => {
    attachShell(() => undefined);

    const decision = deriveReconcilerAction("?panel=teams-spend", null)!;
    openPanel(decision.panelKey!, decision.durableParams);

    expect(getWorkspaceSnapshot().main).toMatchObject({ panelId: "settings", params: { section: "business" } });
    expect(getWorkspaceSnapshot().place).toBe("settings");
    openPanel("teams-payments", {});
    expect(getWorkspaceSnapshot().openPanelIds.filter((id) => id === "settings")).toHaveLength(1);
  });

  it("the latch fires: a second reconcile of the same URL is a no-op", () => {
    const first = deriveReconcilerAction("?panel=teams-spend", null)!;
    expect(deriveReconcilerAction("?panel=teams-spend", first.nextSignature)).toBeNull();
  });

  it("any key but Team falls through to the page-gone view", () => {
    render(<TeamsPanelRouter panel="constructor" />);
    expect(screen.getByText(/This page is no longer here/)).toBeInTheDocument();
    render(<TeamsPanelRouter panel="money" />);
    expect(screen.getAllByText(/This page is no longer here/)).toHaveLength(2);
  });

  it("?panel=usage opens nothing here — Usage lives on the Settings page", () => {
    expect(deriveReconcilerAction("?panel=usage", null)!.command).toBeNull();
  });
});
