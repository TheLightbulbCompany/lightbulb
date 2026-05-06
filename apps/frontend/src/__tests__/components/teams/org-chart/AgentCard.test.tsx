import { describe, test, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AgentCard } from "@/components/teams/org-chart/AgentCard";

const baseProps = {
  id: "ag_1",
  name: "Main Agent",
  role: "ceo",
  status: "idle",
  x: 100,
  y: 50,
};

describe("AgentCard", () => {
  test("renders agent name", () => {
    render(<AgentCard {...baseProps} />);
    expect(screen.getByText("Main Agent")).toBeInTheDocument();
  });

  test("renders role label (capitalized via Tailwind)", () => {
    render(<AgentCard {...baseProps} />);
    expect(screen.getByText("ceo")).toBeInTheDocument();
  });

  test("renders status dot with aria-label and title", () => {
    render(<AgentCard {...baseProps} status="running" />);
    const dot = screen.getByLabelText(/status: running/i);
    expect(dot).toBeInTheDocument();
    // Active states pulse to indicate live activity.
    expect(dot.className).toMatch(/animate-pulse/);
    // 'running' resolves to the editorial forest token.
    expect(dot).toHaveStyle({ backgroundColor: "var(--forest)" });
  });

  test("status dot uses default color for unknown status", () => {
    render(<AgentCard {...baseProps} status="weird" />);
    const dot = screen.getByLabelText(/status: weird/i);
    // Unknown statuses fall back to the editorial mute token.
    expect(dot).toHaveStyle({ backgroundColor: "var(--mute)" });
  });

  test("link href points to /teams/agents/{id}", () => {
    render(<AgentCard {...baseProps} />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/teams/agents/ag_1");
  });

  test("position style applies x/y from props", () => {
    render(<AgentCard {...baseProps} x={250} y={400} />);
    const link = screen.getByRole("link");
    expect(link).toHaveStyle({ left: "250px", top: "400px" });
  });

  test("data-agent-card-id attribute set for test selectors", () => {
    const { container } = render(<AgentCard {...baseProps} />);
    expect(container.querySelector('[data-agent-card-id="ag_1"]')).toBeInTheDocument();
  });
});
