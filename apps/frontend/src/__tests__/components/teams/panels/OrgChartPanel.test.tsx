import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { OrgChartAgent } from "@/components/teams/org-chart/orgChartLayout";

const mockRead = vi.fn();
const mockOrgChart = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

vi.mock("@/components/teams/org-chart/OrgChart", () => ({
  OrgChart: ({ agents }: { agents: OrgChartAgent[] }) => {
    mockOrgChart(agents);
    return <div data-testid="org-chart">{agents.map((agent) => agent.name).join(", ")}</div>;
  },
}));

import { OrgChartPanel } from "@/components/teams/panels/OrgChartPanel";

describe("OrgChartPanel", () => {
  beforeEach(() => {
    mockRead.mockReset();
    mockOrgChart.mockReset();
  });

  test("renders loading state", () => {
    mockRead.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });

    render(<OrgChartPanel />);

    expect(mockRead).toHaveBeenCalledWith("/agents");
    expect(screen.getByText("Loading...")).toBeInTheDocument();
    expect(mockOrgChart).not.toHaveBeenCalled();
  });

  test("renders error state", () => {
    mockRead.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error("boom"),
    });

    render(<OrgChartPanel />);

    expect(screen.getByRole("alert")).toHaveTextContent("Failed to load agents.");
    expect(mockOrgChart).not.toHaveBeenCalled();
  });

  test("renders OrgChart with normalized agents", () => {
    const agents = [
      { id: "ceo", name: "CEO", role: "ceo", reportsTo: null, status: "idle" },
      { id: "eng", name: "Engineer", role: "engineer", reportsTo: "ceo", status: "running" },
    ] as OrgChartAgent[];

    mockRead.mockReturnValue({
      data: { agents },
      isLoading: false,
      error: null,
    });

    render(<OrgChartPanel />);

    expect(screen.getByRole("heading", { name: "Org chart" })).toBeInTheDocument();
    expect(screen.getByTestId("org-chart")).toHaveTextContent("CEO, Engineer");
    expect(mockOrgChart).toHaveBeenCalledWith(agents);
  });
});
