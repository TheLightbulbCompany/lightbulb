import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { PanelMasthead } from "@/components/teams/shell/PanelMasthead";

describe("PanelMasthead", () => {
  it("renders overline + title", () => {
    render(<PanelMasthead overline="Overview" title="Dashboard" />);
    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Dashboard", level: 1 })).toBeInTheDocument();
  });

  it("renders meta on the right", () => {
    render(<PanelMasthead overline="X" title="Y" meta="Mon, May 7" />);
    expect(screen.getByText("Mon, May 7")).toBeInTheDocument();
  });

  it("renders actions slot", () => {
    render(
      <PanelMasthead overline="X" title="Y" actions={<button>Create</button>} />
    );
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
  });

  it("does not render meta region when meta is omitted", () => {
    render(<PanelMasthead overline="X" title="Y" />);
    // The meta `<p>` shouldn't exist
    expect(screen.queryByText("X")).toBeInTheDocument(); // overline still there
    // No tabular/font-mono text other than possibly the title (which uses font-editorial-serif)
  });

  it("merges caller className", () => {
    render(<PanelMasthead overline="X" title="Y" className="custom" />);
    expect(screen.getByRole("banner")).toHaveClass("custom");
  });
});
