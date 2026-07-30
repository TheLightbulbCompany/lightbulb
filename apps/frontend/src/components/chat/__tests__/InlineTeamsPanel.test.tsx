import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { isTeamsPanelKey, InlineTeamsPanel } from "../InlineTeamsPanel";

// Drives the workspace-status gate. Prefixed `mock*` so vi.mock's hoisted
// factory may reference it.
const mockWorkspaceStatus: { current: { kind: string; error?: Error } } = {
  current: { kind: "ready" },
};
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsWorkspaceStatus: () => mockWorkspaceStatus.current,
}));

// Teams workspace-status overlays — stub to identifiable nodes.
vi.mock("@/components/teams/TeamsStatusOverlays", () => ({
  ProvisioningOverlay: () => <div data-testid="provisioning-overlay" />,
  SubscribeOverlay: () => <div data-testid="subscribe-overlay" />,
  ErrorOverlay: () => <div data-testid="error-overlay" />,
}));

// The real providers pull in the gateway/SWR; stub them to pass-throughs.
vi.mock("@/components/teams/TeamsEventsProvider", () => ({
  TeamsEventsProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/teams/command-palette/CommandPaletteProvider", () => ({
  CommandPaletteProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/teams/TeamsPanelRouter", () => ({
  TeamsPanelRouter: ({
    panel,
    detailId,
  }: {
    panel: string;
    detailId?: string | null;
  }) => (
    <div data-testid="teams-panel" data-detail-id={detailId ?? ""}>
      {panel}
    </div>
  ),
}));

beforeEach(() => {
  mockWorkspaceStatus.current = { kind: "ready" };
});

describe("isTeamsPanelKey", () => {
  it("matches teams- prefixed keys only", () => {
    expect(isTeamsPanelKey("teams-dashboard")).toBe(true);
    expect(isTeamsPanelKey("overview")).toBe(false);
  });
});

describe("InlineTeamsPanel", () => {
  it("strips the teams- prefix before delegating to TeamsPanelRouter (when ready)", () => {
    render(<InlineTeamsPanel panel="teams-dashboard" />);
    expect(screen.getByTestId("teams-panel")).toHaveTextContent("dashboard");
  });
  it("shows the provisioning overlay (not the panel) while the workspace provisions", () => {
    mockWorkspaceStatus.current = { kind: "provisioning" };
    render(<InlineTeamsPanel panel="teams-dashboard" />);
    expect(screen.getByTestId("provisioning-overlay")).toBeInTheDocument();
    expect(screen.queryByTestId("teams-panel")).toBeNull();
  });
  it("shows the subscribe overlay when a subscription is required", () => {
    mockWorkspaceStatus.current = { kind: "subscribe_required" };
    render(<InlineTeamsPanel panel="teams-dashboard" />);
    expect(screen.getByTestId("subscribe-overlay")).toBeInTheDocument();
    expect(screen.queryByTestId("teams-panel")).toBeNull();
  });
  it("makes its scrollport a containing block for out-of-flow panel content", () => {
    // `relative` is load-bearing: this host and ChatLayout's `.main-content`
    // around it are both position:static, so without it a panel's absolutely
    // positioned descendants (Tailwind `sr-only`) resolve their containing
    // block past this scrollport and inflate an outer scroller with phantom
    // height — the same defect the dock host fixes in workspace.css.
    const { container } = render(<InlineTeamsPanel panel="teams-dashboard" />);
    const scrollport = container.querySelector(".editorial-page") as HTMLElement;
    expect(scrollport.classList.contains("relative")).toBe(true);
    expect(scrollport.classList.contains("overflow-auto")).toBe(true);
  });
  it("threads detailId through to TeamsPanelRouter", () => {
    render(
      <InlineTeamsPanel panel="teams-issue-detail" detailId="iss_42" />,
    );
    const panel = screen.getByTestId("teams-panel");
    expect(panel).toHaveTextContent("issue-detail");
    expect(panel).toHaveAttribute("data-detail-id", "iss_42");
  });
});
