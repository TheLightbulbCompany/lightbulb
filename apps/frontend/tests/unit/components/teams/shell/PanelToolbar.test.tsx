import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PanelToolbar } from "@/components/teams/shell/PanelToolbar";

describe("PanelToolbar", () => {
  it("renders the search placeholder", () => {
    render(
      <PanelToolbar searchValue="" onSearchChange={() => {}} searchPlaceholder="Find issue..." />
    );
    expect(screen.getByPlaceholderText("Find issue...")).toBeInTheDocument();
  });

  it("calls onSearchChange when typing", async () => {
    const onSearchChange = vi.fn();
    render(
      <PanelToolbar searchValue="" onSearchChange={onSearchChange} searchPlaceholder="search" />
    );
    await userEvent.type(screen.getByPlaceholderText("search"), "h");
    expect(onSearchChange).toHaveBeenCalledWith("h");
  });

  it("renders the trailing actions slot", () => {
    render(
      <PanelToolbar
        searchValue=""
        onSearchChange={() => {}}
        searchPlaceholder="x"
        actions={<button>+ Create</button>}
      />
    );
    expect(screen.getByRole("button", { name: "+ Create" })).toBeInTheDocument();
  });

  it("renders the filters slot", () => {
    render(
      <PanelToolbar
        searchValue=""
        onSearchChange={() => {}}
        searchPlaceholder="x"
        filters={<span data-testid="filters">F</span>}
      />
    );
    expect(screen.getByTestId("filters")).toBeInTheDocument();
  });

  it("displays the controlled searchValue", () => {
    render(
      <PanelToolbar searchValue="hello" onSearchChange={() => {}} searchPlaceholder="x" />
    );
    expect(screen.getByPlaceholderText("x")).toHaveValue("hello");
  });

  it("merges caller className", () => {
    const { container } = render(
      <PanelToolbar searchValue="" onSearchChange={() => {}} searchPlaceholder="x" className="custom" />
    );
    expect(container.firstChild).toHaveClass("custom");
  });
});
