import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusChip } from "../StatusChip";

describe("StatusChip", () => {
  it("renders the status label and an emerald class for running", () => {
    const { container } = render(<StatusChip status="running" />);
    expect(screen.getByText(/running/i)).toBeTruthy();
    expect(container.querySelector("span")?.className).toContain("emerald");
  });

  it("renders a zinc (unknown) class for an unrecognized status", () => {
    const { container } = render(<StatusChip status="wat" />);
    expect(container.querySelector("span")?.className).toContain("zinc");
  });
});
