import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SystemNav } from "../SystemNav";

// SystemNav reuses ControlSidebar's admin gating (Usage hidden from non-admin
// org members), which reads Clerk org membership. Seed it per test.
const mockMembership = vi.fn();
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ membership: mockMembership() }),
}));

describe("SystemNav", () => {
  it("renders the global control panels under a System section label", () => {
    mockMembership.mockReturnValue(null); // personal owner (admin)
    render(<SystemNav />);
    expect(screen.getByText("System")).toBeInTheDocument();
    expect(screen.getByText("Sessions")).toBeInTheDocument();
    expect(screen.getByText("Skills")).toBeInTheDocument();
    expect(screen.getByText("Integrations")).toBeInTheDocument();
    expect(screen.getByText("Overview")).toBeInTheDocument();
  });

  it("excludes the Agents item (covered by the per-agent inspector)", () => {
    mockMembership.mockReturnValue(null);
    render(<SystemNav />);
    expect(screen.queryByText("Agents")).not.toBeInTheDocument();
  });

  it("emits the plain (non-teams-prefixed) panel key on click", () => {
    mockMembership.mockReturnValue(null);
    const onPanelChange = vi.fn();
    render(<SystemNav onPanelChange={onPanelChange} />);
    fireEvent.click(screen.getByText("Sessions"));
    expect(onPanelChange).toHaveBeenCalledWith("sessions");
  });

  it("hides Usage from non-admin org members", () => {
    mockMembership.mockReturnValue({ role: "org:member" });
    render(<SystemNav />);
    expect(screen.queryByText("Usage")).not.toBeInTheDocument();
  });

  it("shows Usage to org admins", () => {
    mockMembership.mockReturnValue({ role: "org:admin" });
    render(<SystemNav />);
    expect(screen.getByText("Usage")).toBeInTheDocument();
  });

  it("marks the active item with the forest accent", () => {
    mockMembership.mockReturnValue(null);
    render(<SystemNav activePanel="sessions" />);
    const btn = screen.getByText("Sessions").closest("button");
    expect(btn?.className).toMatch(/forest/);
  });

  it("is a labeled, shrinkable scroll region (a11y + clip-safety, mirrors CompanyNav)", () => {
    mockMembership.mockReturnValue(null);
    render(<SystemNav />);
    const nav = screen.getByRole("navigation", { name: "System" });
    expect(nav.className).toMatch(/min-h-0/);
    expect(nav.className).toMatch(/overflow-y-auto/);
  });
});
