import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusChip } from "../StatusChip";

describe("StatusChip", () => {
  it("renders the status label and the ok tone for running", () => {
    const { container } = render(<StatusChip status="running" />);
    expect(screen.getByText(/running/i)).toBeTruthy();
    expect(container.querySelector("span")?.className).toContain("text-ok");
  });

  it("renders the neutral (unknown) tone for an unrecognized status", () => {
    const { container } = render(<StatusChip status="wat" />);
    expect(container.querySelector("span")?.className).toContain("text-muted-foreground");
  });
});
