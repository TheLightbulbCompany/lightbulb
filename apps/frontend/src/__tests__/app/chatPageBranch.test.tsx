// Pins the /chat page's desktop/mobile branch (Task 13):
//   - useIsDesktop() === true  → WorkspaceShell mounts, ChatLayout does NOT,
//     and the desktop RunNotifications variant (mode="desktop") mounts.
//   - useIsDesktop() === false → ChatLayout (the mobile shell) mounts,
//     WorkspaceShell does NOT, and the mobile RunNotifications variant
//     (mode="mobile") mounts.
// SSR renders mobile (useIsDesktop's server snapshot is false); the desktop
// shell swaps in on hydration. Only ONE branch renders at a time, so exactly
// one RunNotifications is live.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

// Controllable desktop/mobile switch.
const view = vi.hoisted(() => ({ isDesktop: false }));
vi.mock("@/hooks/useIsDesktop", () => ({
  useIsDesktop: () => view.isDesktop,
}));

// The two shells reduced to inert markers — we assert which one mounts, not
// their internals (both are covered by their own suites).
vi.mock("@/components/workspace/WorkspaceShell", () => ({
  WorkspaceShell: () => <div data-testid="workspace-shell" />,
}));
vi.mock("@/components/chat/ChatLayout", () => ({
  // Ignore children so the mobile shell's AgentChatWindow / ControlPanelRouter
  // subtree never mounts (no gateway, no RPC) — the marker is all we assert.
  ChatLayout: () => <div data-testid="chat-layout" />,
}));

// RunNotifications renders nothing in prod; here it echoes its mode so the test
// can assert the correct per-branch variant mounted.
vi.mock("@/components/notifications/RunNotifications", () => ({
  RunNotifications: (props: { mode: string }) => (
    <div data-testid="run-notifications" data-mode={props.mode} />
  ),
}));

// Providers → pass-throughs (no WebSocket, no drawer context churn).
vi.mock("@/hooks/useGateway", () => ({
  GatewayProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useGateway: () => ({}),
}));
vi.mock("@/hooks/useExecApprovals", () => ({
  ExecApprovalsProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  MarketplaceDrawerProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useMarketplaceDrawer: () => ({ open: vi.fn() }),
}));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawer", () => ({
  MarketplaceDrawer: () => null,
}));
vi.mock("@/components/marketplace/drawer/MarketplaceToast", () => ({
  MarketplaceToast: () => null,
}));
// Idle preload effect (both shells) — no-op it so no timer/chunk fetch fires.
vi.mock("@/components/teams/TeamsPanelRouter", () => ({
  preloadTeamsPanels: vi.fn(),
}));

import ChatPage from "@/app/chat/page";

describe("/chat page — desktop/mobile branch", () => {
  beforeEach(() => {
    view.isDesktop = false;
  });

  it("desktop: mounts WorkspaceShell (mode=desktop notifications), no ChatLayout", () => {
    view.isDesktop = true;
    render(<ChatPage />);
    expect(screen.getByTestId("workspace-shell")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-layout")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-notifications")).toHaveAttribute("data-mode", "desktop");
  });

  it("mobile: mounts ChatLayout (mode=mobile notifications), no WorkspaceShell", () => {
    view.isDesktop = false;
    render(<ChatPage />);
    expect(screen.getByTestId("chat-layout")).toBeInTheDocument();
    expect(screen.queryByTestId("workspace-shell")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-notifications")).toHaveAttribute("data-mode", "mobile");
  });

  it("exactly one shell and one notifications mount at a time", () => {
    view.isDesktop = true;
    render(<ChatPage />);
    expect(screen.getAllByTestId("run-notifications")).toHaveLength(1);
  });
});
