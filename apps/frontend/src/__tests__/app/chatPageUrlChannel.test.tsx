// The /chat URL is a one-way command channel with a SINGLE owner, and the
// mobile shell is not it.
//
// The bug this pins: MobileChatShell used to canonicalize a retired ?panel key
// in the address bar (replace-only). On a DESKTOP viewport that rewrite still
// ran — useIsDesktop's server snapshot is false, so ChatPageInner mounts the
// mobile branch for the hydration render (chatPageBranch.test.tsx pins that
// branch contract), React flushes that child's passive effects, and only later
// does the dynamically-imported WorkspaceShell load and read
// window.location.search. By then the raw key was gone, so describePanel's
// pre-alias branch — the thing that carries a retired panel's meaning onto its
// successor — could never fire, and the successor opened on its default view.
// (It was ?panel=dreams → Brain's Dreams tab then; today the live case is
// ?panel=teams-org-chart → the Agents panel's chart view.)
//
// workspaceShellUrl.test.ts already covers the dock's chain, but it feeds
// deriveReconcilerAction a hardcoded search string and never mounts ChatPage,
// so it stayed green throughout. These tests mount the real page and assert
// against the LIVE window.location.search, which is where the rewrite was
// observable.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import React from "react";

// Faithful to the App Router: useSearchParams tracks the live query string
// (shellNav commits through pushState/replaceState precisely because the
// router updates in place). shellNav itself is deliberately NOT mocked — a
// rewrite must really move window.location.
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

// Mobile branch: the one that mounts first on every viewport.
vi.mock("@/hooks/useIsDesktop", () => ({ useIsDesktop: () => false }));

// ChatLayout reduced to a pass-through so the inline Teams mount below is
// reached (the sibling chatPageBranch.test.tsx swallows children instead —
// it asserts which shell mounts, not what the shell threads down).
// The shell hands ChatLayout its `onPanelChange` (setActivePanel) — captured
// so a test can perform a plain sidebar nav without mounting the real nav.
const layout = vi.hoisted(() => ({ onPanelChange: (() => {}) as (next: string) => void }));
vi.mock("@/components/chat/ChatLayout", () => ({
  ChatLayout: ({
    children,
    onPanelChange,
  }: {
    children: React.ReactNode;
    onPanelChange?: (next: string) => void;
  }) => {
    if (onPanelChange) layout.onPanelChange = onPanelChange;
    return <div>{children}</div>;
  },
}));
vi.mock("@/components/chat/AgentChatWindow", () => ({ AgentChatWindow: () => null }));
vi.mock("@/components/notifications/RunNotifications", () => ({ RunNotifications: () => null }));
// Echo the params the shell derives from the URL; keep the real key helpers.
vi.mock("@/components/chat/InlineTeamsPanel", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/chat/InlineTeamsPanel")>()),
  InlineTeamsPanel: (props: { panel: string; panelParams?: Record<string, string> }) => (
    <div
      data-testid="inline-teams"
      data-panel={props.panel}
      data-tab={props.panelParams?.tab ?? ""}
      data-view={props.panelParams?.view ?? ""}
    />
  ),
}));

vi.mock("@/hooks/useGateway", () => ({
  GatewayProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  // Shape-faithful: MobileChatShell → useAgents → useRuntimeRpc destructures
  // these and calls onEvent in a passive effect.
  useGateway: () => ({
    sendReq: vi.fn().mockResolvedValue({}),
    onEvent: () => () => {},
    onChatMessage: () => () => {},
    isParked: false,
    isConnected: false,
  }),
}));
vi.mock("@/hooks/useExecApprovals", () => ({
  ExecApprovalsProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  MarketplaceDrawerProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useMarketplaceDrawer: () => ({ open: vi.fn() }),
}));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawer", () => ({ MarketplaceDrawer: () => null }));
vi.mock("@/components/marketplace/drawer/MarketplaceToast", () => ({ MarketplaceToast: () => null }));

import ChatPage from "@/app/chat/page";
import { describePanel } from "@/components/workspace/panelRegistry";
import { deriveReconcilerAction } from "@/components/workspace/workspaceShellReconciler";
import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

describe("/chat — a retired ?panel=teams-org-chart lands on the Agents chart view", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/chat?panel=teams-org-chart");
  });

  it("leaves the retired key in the address bar, and lands mobile on the chart", () => {
    render(<ChatPage />);
    expect(window.location.search).toBe("?panel=teams-org-chart");
    const inline = screen.getByTestId("inline-teams");
    expect(inline).toHaveAttribute("data-panel", "teams-agents"); // resolved at READ time
    expect(inline).toHaveAttribute("data-view", "chart");
  });

  it("a plain sidebar nav clears the view — mobile has no clearablePanelParamKeys", () => {
    render(<ChatPage />);
    act(() => layout.onPanelChange("teams-issues"));
    const params = new URLSearchParams(window.location.search);
    expect(params.get("panel")).toBe("teams-issues");
    expect(params.get("view")).toBeNull();
  });

  it("desktop dock resolves it to ONE Agents panel on the chart view", () => {
    render(<ChatPage />);
    const decision = deriveReconcilerAction(window.location.search, null)!;
    const descriptor = describePanel(decision.panelKey!, decision.durableParams)!;
    expect(descriptor.panelId).toBe("teams-agents");

    const element = TeamsPanelRouter({
      panel: descriptor.params.teamsKey,
      panelParams: descriptor.params,
    }) as React.ReactElement<{ initialView: string | null }>;
    expect(element.props.initialView).toBe("chart");
  });
});
