import { describe, test, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { OrgChart } from "@/components/teams/org-chart/OrgChart";
import type { OrgChartAgent } from "@/components/teams/org-chart/orgChartLayout";

describe("OrgChart", () => {
  test("renders empty state when no agents", () => {
    render(<OrgChart agents={[]} />);
    expect(screen.getByText(/no agents yet/i)).toBeInTheDocument();
  });

  test("renders a single root agent", () => {
    const agents: OrgChartAgent[] = [
      { id: "ag_1", name: "Main Agent", role: "ceo", reportsTo: null, status: "idle" } as OrgChartAgent,
    ];
    render(<OrgChart agents={agents} />);
    expect(screen.getByText("Main Agent")).toBeInTheDocument();
    expect(screen.queryByText(/no agents yet/i)).not.toBeInTheDocument();
  });

  test("renders all cards across multi-agent tree", () => {
    const agents: OrgChartAgent[] = [
      { id: "ceo", name: "Boss", role: "ceo", reportsTo: null, status: "idle" } as OrgChartAgent,
      { id: "eng_1", name: "Engineer One", role: "engineer", reportsTo: "ceo", status: "running" } as OrgChartAgent,
      { id: "eng_2", name: "Engineer Two", role: "engineer", reportsTo: "ceo", status: "idle" } as OrgChartAgent,
    ];
    const { container } = render(<OrgChart agents={agents} />);
    const cards = container.querySelectorAll("[data-agent-card-id]");
    expect(cards).toHaveLength(3);
  });

  test("renders SVG edges = (agents - roots)", () => {
    const agents: OrgChartAgent[] = [
      { id: "ceo", name: "Boss", role: "ceo", reportsTo: null, status: "idle" } as OrgChartAgent,
      { id: "eng_1", name: "Engineer", role: "engineer", reportsTo: "ceo", status: "idle" } as OrgChartAgent,
      { id: "eng_2", name: "Designer", role: "designer", reportsTo: "ceo", status: "idle" } as OrgChartAgent,
    ];
    // 3 agents, 1 root → 2 edges
    const { container } = render(<OrgChart agents={agents} />);
    const edges = container.querySelectorAll("[data-org-edge]");
    expect(edges).toHaveLength(2);
  });

  test("renders 0 edges when all agents are independent roots", () => {
    const agents: OrgChartAgent[] = [
      { id: "a1", name: "Agent 1", role: "x", reportsTo: null, status: "idle" } as OrgChartAgent,
      { id: "a2", name: "Agent 2", role: "x", reportsTo: null, status: "idle" } as OrgChartAgent,
    ];
    const { container } = render(<OrgChart agents={agents} />);
    const edges = container.querySelectorAll("[data-org-edge]");
    expect(edges).toHaveLength(0);
  });

  test("renders a pan/zoom viewport wrapping the canvas", () => {
    const agents: OrgChartAgent[] = [
      { id: "ag_1", name: "Test", role: "ceo", reportsTo: null, status: "idle" } as OrgChartAgent,
    ];
    const { getByTestId } = render(<OrgChart agents={agents} />);
    // The viewport hosts the pan/zoom transform; the canvas carries it.
    expect(getByTestId("org-chart-viewport")).toBeInTheDocument();
    const canvas = getByTestId("org-chart-canvas");
    // In jsdom the container reports 0 size, so fit-to-screen is a no-op and
    // the canvas transform stays at the 1x identity (no pan, no scale).
    expect(canvas.getAttribute("style")).toContain("translate(0px, 0px) scale(1)");
  });

  test("exposes zoom controls", () => {
    const agents: OrgChartAgent[] = [
      { id: "ag_1", name: "Test", role: "ceo", reportsTo: null, status: "idle" } as OrgChartAgent,
    ];
    render(<OrgChart agents={agents} />);
    expect(screen.getByLabelText(/zoom in/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/zoom out/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/fit chart to screen/i)).toBeInTheDocument();
  });

  test("cards are positioned via inline style", () => {
    const agents: OrgChartAgent[] = [
      { id: "root", name: "Root", role: "ceo", reportsTo: null, status: "idle" } as OrgChartAgent,
    ];
    const { container } = render(<OrgChart agents={agents} />);
    const card = container.querySelector("[data-agent-card-id='root']") as HTMLElement;
    // PADDING=60, single root: x = 60, y = 60. (The pan/zoom transform lives
    // on the canvas layer, not the card, so the card keeps its layout coords.)
    expect(card.getAttribute("style")).toContain("left: 60px");
    expect(card.getAttribute("style")).toContain("top: 60px");
  });
});
