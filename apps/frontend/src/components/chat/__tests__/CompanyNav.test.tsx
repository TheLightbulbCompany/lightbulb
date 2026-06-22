import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CompanyNav } from "../CompanyNav";

describe("CompanyNav", () => {
  it("renders the Dashboard and Activity items", () => {
    render(<CompanyNav />);
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Activity")).toBeInTheDocument();
  });
  it("emits the teams- panel key on click", () => {
    const onPanelChange = vi.fn();
    render(<CompanyNav onPanelChange={onPanelChange} />);
    fireEvent.click(screen.getByText("Dashboard"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-dashboard");
  });
  it("marks the active item with the forest accent", () => {
    render(<CompanyNav activePanel="teams-activity" />);
    const btn = screen.getByText("Activity").closest("button");
    expect(btn?.className).toMatch(/forest/);
  });
});
