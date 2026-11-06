// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { TeamsPanelRouter } from "../TeamsPanelRouter";

describe("TeamsPanelRouter", () => {
  it("renders the unknown-panel line for a deleted key instead of crashing", () => {
    // A bookmark or an agent's old link to a deleted tab lands here.
    render(<TeamsPanelRouter panel="leads" />);
    expect(screen.getByText("This page is no longer here.")).toBeInTheDocument();
  });
});
