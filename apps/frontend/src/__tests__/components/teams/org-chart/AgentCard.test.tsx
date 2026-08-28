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
    expect(dot).toHaveStyle({ backgroundColor: "var(--midnight)" });
  });

  test("status dot uses default color for unknown status", () => {
    render(<AgentCard {...baseProps} status="weird" />);
    const dot = screen.getByLabelText(/status: weird/i);
    // Unknown statuses fall back to the editorial mute token.
    expect(dot).toHaveStyle({ backgroundColor: "var(--midnight-muted)" });
  });

  test("link href points to the shell's agent inspector", () => {
    render(<AgentCard {...baseProps} />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/chat?panel=agent-inspector&agent=ag_1");
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

  test("data-org-card marker present so the viewport skips drag on card click", () => {
    const { container } = render(<AgentCard {...baseProps} />);
    expect(container.querySelector("[data-org-card]")).toBeInTheDocument();
  });

  // ── Enrichment ──

  test("renders title in place of role when present", () => {
    render(<AgentCard {...baseProps} title="Chief Executive" />);
    expect(screen.getByText("Chief Executive")).toBeInTheDocument();
    // role is not shown as the subtitle once a title is supplied
    expect(screen.queryByText("ceo")).not.toBeInTheDocument();
  });

  test("falls back to role as subtitle when title is empty/whitespace", () => {
    render(<AgentCard {...baseProps} title="   " />);
    expect(screen.getByText("ceo")).toBeInTheDocument();
  });

  test("renders capabilities text", () => {
    render(<AgentCard {...baseProps} capabilities="Writes and ships backend code" />);
    expect(screen.getByText("Writes and ships backend code")).toBeInTheDocument();
  });

  test("omits capabilities block when absent", () => {
    render(<AgentCard {...baseProps} capabilities={null} />);
    // Only the subtitle ('ceo') should be present; no extra body line.
    expect(screen.queryByText(/writes and ships/i)).not.toBeInTheDocument();
  });

  test("renders a glyph avatar (no image) — Agent.icon is opaque, not a URL", () => {
    const { container } = render(<AgentCard {...baseProps} />);
    // No <img> avatar plumbing — every card uses the Bot glyph (an SVG).
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("svg")).toBeInTheDocument();
  });

  test("renders the status label in the footer (capitalized)", () => {
    render(<AgentCard {...baseProps} status="running" />);
    // status footer + dot title both carry the humanized label
    expect(screen.getAllByText("Running").length).toBeGreaterThan(0);
  });

  test("appends the error reason to the status when errored", () => {
    render(
      <AgentCard {...baseProps} status="error" errorReason="adapter timeout" />,
    );
    expect(screen.getAllByText(/Error · adapter timeout/).length).toBeGreaterThan(0);
  });

  test("maps the pause-reason code to prose when paused", () => {
    // Upstream sends the enum code ("budget"); the card humanizes it.
    render(<AgentCard {...baseProps} status="paused" pauseReason="budget" />);
    expect(screen.getAllByText(/Paused · budget exhausted/).length).toBeGreaterThan(0);
    // The raw code must not leak into the rendered label.
    expect(screen.queryByText(/Paused · budget$/)).not.toBeInTheDocument();
  });

  test("falls back to the raw pause code for an unknown reason", () => {
    render(<AgentCard {...baseProps} status="paused" pauseReason="mystery" />);
    expect(screen.getAllByText(/Paused · mystery/).length).toBeGreaterThan(0);
  });

  test("renders a relative last-active line from lastHeartbeatAt", () => {
    const tenMinAgo = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    render(<AgentCard {...baseProps} lastHeartbeatAt={tenMinAgo} />);
    expect(screen.getByText("10m ago")).toBeInTheDocument();
  });

  test("omits the last-active line when lastHeartbeatAt is absent", () => {
    render(<AgentCard {...baseProps} lastHeartbeatAt={null} />);
    expect(screen.queryByText(/ago/i)).not.toBeInTheDocument();
  });
});
