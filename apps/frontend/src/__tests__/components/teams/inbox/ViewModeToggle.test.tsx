import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ViewModeToggle } from "@/components/teams/inbox/ViewModeToggle";

describe("ViewModeToggle", () => {
  it("renders both buttons", () => {
    render(<ViewModeToggle value="list" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: /list/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /board/i })).toBeInTheDocument();
  });

  it("marks the active button with aria-pressed=true", () => {
    render(<ViewModeToggle value="board" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: /board/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /list/i })).toHaveAttribute("aria-pressed", "false");
  });

  it("calls onChange('board') when board button clicked from list mode", async () => {
    const onChange = vi.fn();
    render(<ViewModeToggle value="list" onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: /board/i }));
    expect(onChange).toHaveBeenCalledWith("board");
  });

  it("does not call onChange when the active button is clicked", async () => {
    const onChange = vi.fn();
    render(<ViewModeToggle value="list" onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: /list/i }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
