import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Monogram, StatusDot, AgentPill, SectionLabel } from "../AgentIdentity";

describe("Monogram", () => {
  it("renders the monogram glyph and applies accent + tint", () => {
    render(<Monogram agent={{ monogram: "At", accent: "#06402B", tint: "#e6efe9" }} size={32} />);
    const node = screen.getByText("At");
    expect(node).toHaveStyle({ color: "#06402B", backgroundColor: "#e6efe9" });
  });
});

describe("StatusDot", () => {
  it("defaults to the active variant", () => {
    const { container } = render(<StatusDot />);
    expect(container.querySelector(".status-dot--active")).toBeInTheDocument();
  });

  it.each([
    ["thinking", "status-dot--thinking"],
    ["paused", "status-dot--paused"],
    ["error", "status-dot--error"],
  ])("maps %s to %s", (status, cls) => {
    const { container } = render(<StatusDot status={status} />);
    expect(container.querySelector(`.${cls}`)).toBeInTheDocument();
  });
});

describe("AgentPill", () => {
  it("applies the forest tone modifier class", () => {
    const { container } = render(<AgentPill tone="forest">Live</AgentPill>);
    expect(container.querySelector(".agent-pill--forest")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
  });
});

describe("SectionLabel", () => {
  it("renders label without count", () => {
    render(<SectionLabel label="My Agents" />);
    expect(screen.getByText("My Agents")).toBeInTheDocument();
  });

  it("renders count when provided", () => {
    render(<SectionLabel label="My Agents" count={6} />);
    expect(screen.getByText("6")).toBeInTheDocument();
  });
});
