import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { isTeamsPanelKey, InlineTeamsPanel } from "../InlineTeamsPanel";

// The real providers pull in the gateway/SWR; stub them to pass-throughs.
vi.mock("@/components/teams/TeamsEventsProvider", () => ({
  TeamsEventsProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/teams/command-palette/CommandPaletteProvider", () => ({
  CommandPaletteProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/teams/TeamsPanelRouter", () => ({
  TeamsPanelRouter: ({ panel }: { panel: string }) => (
    <div data-testid="teams-panel">{panel}</div>
  ),
}));

describe("isTeamsPanelKey", () => {
  it("matches teams- prefixed keys only", () => {
    expect(isTeamsPanelKey("teams-dashboard")).toBe(true);
    expect(isTeamsPanelKey("overview")).toBe(false);
  });
});

describe("InlineTeamsPanel", () => {
  it("strips the teams- prefix before delegating to TeamsPanelRouter", () => {
    render(<InlineTeamsPanel panel="teams-dashboard" />);
    expect(screen.getByTestId("teams-panel")).toHaveTextContent("dashboard");
  });
});
