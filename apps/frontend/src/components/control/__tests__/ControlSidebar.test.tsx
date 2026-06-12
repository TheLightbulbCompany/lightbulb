import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { ControlSidebar } from "../ControlSidebar";

// The sidebar's only data dependency is the Clerk org membership (admin
// gating for the Usage item). Each test seeds it via this holder.
const mockMembership = vi.fn();
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ membership: mockMembership() }),
}));

describe("ControlSidebar", () => {
  it("renders the consolidated nav: Skills and Model & Credits, no Credits or Capabilities items", () => {
    mockMembership.mockReturnValue(null); // personal owner
    render(<ControlSidebar activePanel="overview" />);
    expect(screen.getByText("Skills")).toBeInTheDocument();
    expect(screen.getByText("Model & Credits")).toBeInTheDocument();
    // Credits merged into Model & Credits; Capabilities flattened into Skills.
    expect(screen.queryByText("Credits")).not.toBeInTheDocument();
    expect(screen.queryByText("Capabilities")).not.toBeInTheDocument();
    expect(screen.queryByText("LLM Provider")).not.toBeInTheDocument();
  });

  it("shows the Channels nav item", () => {
    mockMembership.mockReturnValue(null);
    render(<ControlSidebar activePanel="overview" />);
    expect(screen.getByText("Channels")).toBeInTheDocument();
  });

  it("hides Usage from non-admin org members", () => {
    mockMembership.mockReturnValue({ role: "org:member" });
    render(<ControlSidebar activePanel="overview" />);
    expect(screen.queryByText("Usage")).not.toBeInTheDocument();
  });

  it("shows Usage to org admins", () => {
    mockMembership.mockReturnValue({ role: "org:admin" });
    render(<ControlSidebar activePanel="overview" />);
    expect(screen.getByText("Usage")).toBeInTheDocument();
  });
});
