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

// Stubbed so the mount assertion is about ROUTING, not MoneyPanel's data hooks.
vi.mock("@/components/teams/panels/MoneyPanel", () => ({
  MoneyPanel: (p: { section?: string }) => <div data-testid="money-panel" data-section={p.section ?? ""} />,
}));

import { deriveReconcilerAction } from "../workspaceShellReconciler";
import { openPanel, attachShell, getWorkspaceSnapshot, _resetControllerForTests } from "../workspaceController";
import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

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

  it("executing that decision shows ONE Work view at Money's Spend section", () => {
    attachShell(() => undefined);

    const decision = deriveReconcilerAction("?panel=teams-spend", null)!;
    openPanel(decision.panelKey!, decision.durableParams);

    expect(getWorkspaceSnapshot().main).toMatchObject({
      panelId: "teams-work",
      component: "teams",
      params: { teamsKey: "money", section: "spend" },
    });
    expect(getWorkspaceSnapshot().place).toBe("work");
    // Re-opening, or a DIFFERENT absorbed key, re-targets the same view (one page).
    openPanel("teams-payments", {});
    expect(getWorkspaceSnapshot().openPanelIds.filter((id) => id === "teams-work")).toHaveLength(1);
    expect(getWorkspaceSnapshot().main?.params).toMatchObject({ teamsKey: "money", section: "payments" });
  });

  it("the latch fires: a second reconcile of the same URL is a no-op", () => {
    const first = deriveReconcilerAction("?panel=teams-spend", null)!;
    expect(deriveReconcilerAction("?panel=teams-spend", first.nextSignature)).toBeNull();
  });

  it("TeamsPanelRouter mounts Money at the section the descriptor carries — not the Unknown fallback", async () => {
    const { params } = deriveReconcilerAction("?panel=teams-spend", null)!.command!.descriptor;
    render(<TeamsPanelRouter panel={params.teamsKey} panelParams={{ section: params.section }} />);
    expect(await screen.findByTestId("money-panel")).toHaveAttribute("data-section", "spend");
    expect(screen.queryByText(/This page is no longer here/)).toBeNull();
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
    expect(screen.getByText(/This page is no longer here/)).toBeInTheDocument();
    expect(screen.queryByTestId("money-panel")).toBeNull();
  });

  it("?panel=usage opens nothing here — Usage lives on the Settings page", () => {
    expect(deriveReconcilerAction("?panel=usage", null)!.command).toBeNull();
  });
});
