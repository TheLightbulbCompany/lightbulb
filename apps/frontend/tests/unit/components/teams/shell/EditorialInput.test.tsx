import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditorialInput } from "@/components/teams/shell/EditorialInput";

describe("EditorialInput", () => {
  it("renders label associated with input", () => {
    render(<EditorialInput label="Name" id="name" />);
    expect(screen.getByLabelText("Name")).toBeInTheDocument();
  });

  it("renders helptext", () => {
    render(<EditorialInput label="Email" helpText="we never share" />);
    expect(screen.getByText("we never share")).toBeInTheDocument();
  });

  it("renders error state", () => {
    render(<EditorialInput label="X" error="required" />);
    expect(screen.getByText("required")).toHaveAttribute("role", "alert");
    expect(screen.getByLabelText("X")).toHaveAttribute("aria-invalid", "true");
  });

  it("forwards onChange", async () => {
    const onChange = vi.fn();
    render(<EditorialInput label="X" onChange={onChange} />);
    await userEvent.type(screen.getByLabelText("X"), "hi");
    expect(onChange).toHaveBeenCalled();
  });

  it("forwards ref", () => {
    const ref = { current: null as HTMLInputElement | null };
    render(<EditorialInput label="X" ref={ref} />);
    expect(ref.current).toBeInstanceOf(HTMLInputElement);
    expect(ref.current).toBe(screen.getByLabelText("X"));
  });

  it("merges caller className", () => {
    render(<EditorialInput label="X" className="custom-class" />);
    expect(screen.getByLabelText("X")).toHaveClass("custom-class");
  });
});
