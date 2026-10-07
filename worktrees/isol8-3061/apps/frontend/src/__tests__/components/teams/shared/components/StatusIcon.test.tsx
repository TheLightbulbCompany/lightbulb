import { test, expect } from "vitest";
// Test for StatusIcon — confirms one rendered icon per known IssueStatus
// variant. The component is presentational; no popover interactions.

import { render } from "@testing-library/react";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";

test("StatusIcon renders one icon per known status", () => {
  for (const status of ["todo", "in_progress", "done", "blocked"] as const) {
    const { container } = render(<StatusIcon status={status} />);
    expect(container.querySelector("[aria-label]")).toBeInTheDocument();
  }
});
