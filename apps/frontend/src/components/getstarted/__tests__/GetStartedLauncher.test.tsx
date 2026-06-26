import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GetStartedLauncher } from "../GetStartedLauncher";

describe("GetStartedLauncher", () => {
  it("shows progress and fires onClick", async () => {
    const onClick = vi.fn();
    render(<GetStartedLauncher done={2} total={5} pulse onClick={onClick} />);
    expect(screen.getByText("2/5")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /get started/i }));
    expect(onClick).toHaveBeenCalled();
  });
});
