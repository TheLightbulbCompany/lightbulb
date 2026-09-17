// @vitest-environment jsdom
/**
 * M6 task 10-D, row 21. The container browser has no AgentCore equivalent —
 * the rail drops the Browser destination outright once AgentCore is confirmed.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

let gate: "loading" | "hidden" | "shown";

vi.mock("@/components/teams/command-palette/CommandPaletteProvider", () => ({
  useCommandPalette: () => ({ open: false, setOpen: vi.fn(), toggle: vi.fn() }),
}));
vi.mock("@/components/support/SupportProvider", () => ({ useSupport: () => ({ openSupport: vi.fn() }) }));
vi.mock("@clerk/nextjs", () => ({ UserButton: () => <div aria-label="Profile" /> }));
vi.mock("../workspaceController", () => ({
  openPanel: vi.fn(),
  activateMessagesPanel: vi.fn(),
  useWorkspaceState: () => ({ activePanelId: "teams-dashboard" }),
}));
vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return { ...actual, useHiddenOnAgentCore: () => gate };
});

import { WorkspaceHeader } from "../WorkspaceHeader";

describe("WorkspaceHeader — Browser destination on AgentCore", () => {
  beforeEach(() => {
    gate = "hidden";
  });

  it("drops Browser from the rail once AgentCore is confirmed", () => {
    render(<WorkspaceHeader />);
    expect(screen.queryByRole("button", { name: "Browser" })).toBeNull();
    // Everything else on the rail is untouched.
    expect(screen.getByRole("button", { name: "Browse workspace files" })).toBeInTheDocument();
  });

  it("still offers Browser while the host isn't known yet", () => {
    gate = "loading";
    render(<WorkspaceHeader />);
    expect(screen.getByRole("button", { name: "Browser" })).toBeInTheDocument();
  });

  it("still offers Browser on ECS", () => {
    gate = "shown";
    render(<WorkspaceHeader />);
    expect(screen.getByRole("button", { name: "Browser" })).toBeInTheDocument();
  });
});
