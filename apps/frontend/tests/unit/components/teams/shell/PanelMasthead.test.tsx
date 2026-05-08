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
    expect(screen.getByTestId("panel-masthead-meta")).toHaveTextContent("Mon, May 7");
  });

  it("renders actions slot", () => {
    render(
      <PanelMasthead overline="X" title="Y" actions={<button>Create</button>} />
    );
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
  });

  it("does not render meta when omitted", () => {
    render(<PanelMasthead overline="X" title="Y" />);
    expect(screen.queryByTestId("panel-masthead-meta")).not.toBeInTheDocument();
  });

  it("merges caller className", () => {
    render(<PanelMasthead overline="X" title="Y" className="custom" />);
    expect(screen.getByRole("banner")).toHaveClass("custom");
  });
});
