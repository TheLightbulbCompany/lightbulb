import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditorialSelect } from "@/components/teams/shell/EditorialSelect";

const OPTIONS = [
  { value: "low", label: "Low" },
  { value: "med", label: "Medium" },
  { value: "high", label: "High" },
];

describe("EditorialSelect", () => {
  it("renders the placeholder when no value", () => {
    render(<EditorialSelect options={OPTIONS} placeholder="Pick one" />);
    expect(screen.getByText("Pick one")).toBeInTheDocument();
  });

  it("renders the chosen value's label", () => {
    render(<EditorialSelect options={OPTIONS} value="med" onValueChange={() => {}} />);
    expect(screen.getByText("Medium")).toBeInTheDocument();
  });

  it("calls onValueChange when an option is selected", async () => {
    const onValueChange = vi.fn();
    render(
      <EditorialSelect options={OPTIONS} value="low" onValueChange={onValueChange} />
    );
    await userEvent.click(screen.getByRole("combobox"));
    await userEvent.click(await screen.findByText("High"));
    expect(onValueChange).toHaveBeenCalledWith("high");
  });
});
