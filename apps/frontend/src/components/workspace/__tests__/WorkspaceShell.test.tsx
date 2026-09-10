// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { WorkspaceShell } from "../WorkspaceShell";

const state = vi.hoisted(() => ({ phase: "selecting", selectProps: vi.fn() }));
vi.mock("@/hooks/useOnboardingGate", () => ({ useOnboardingGate: () => state.phase }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isSignedIn: true, userId: "user_1" }),
  useOrganization: () => ({ organization: null }),
  OrganizationList: (props: unknown) => { state.selectProps(props); return null; },
}));
// An owner-aware child must never mount without an active workspace.
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => { throw new Error("Owner data requested without a workspace"); } }));
vi.mock("../useBrowserToolAutoOpen", () => ({ useBrowserToolAutoOpen: () => {} }));
beforeEach(() => { state.phase = "selecting"; vi.clearAllMocks(); });
it("offers Clerk workspace selection instead of an endless loading shell", () => {
  render(<WorkspaceShell />);
  expect(screen.getByRole("heading", { name: "Choose your workspace" })).toBeInTheDocument();
  expect(state.selectProps).toHaveBeenCalledWith(expect.objectContaining({ hidePersonal: true, afterSelectOrganizationUrl: "/chat" }));
});
it("never substitutes the user ID for a missing workspace", () => {
  state.phase = "ready";
  expect(() => render(<WorkspaceShell />)).not.toThrow();
  expect(screen.getByText("Loading…")).toBeInTheDocument();
});
