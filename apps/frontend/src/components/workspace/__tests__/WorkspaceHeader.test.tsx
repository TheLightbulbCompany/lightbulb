// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const setPaletteOpen = vi.fn();

vi.mock("@/components/teams/command-palette/CommandPaletteProvider", () => ({
  useCommandPalette: () => ({ open: false, setOpen: setPaletteOpen, toggle: vi.fn() }),
}));

vi.mock("@/components/support/SupportProvider", () => ({
  useSupport: () => ({ openSupport: vi.fn() }),
}));

vi.mock("@/components/ui/tooltip", () => ({
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@clerk/nextjs", () => {
  const UserButton = Object.assign(
    ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
    {
      MenuItems: ({ children }: { children: React.ReactNode }) => <>{children}</>,
      Link: () => null,
    },
  );
  return { UserButton };
});

vi.mock("../workspaceController", () => ({ openPanel: vi.fn() }));

import { WorkspaceHeader } from "../WorkspaceHeader";

describe("WorkspaceHeader", () => {
  it("opens the existing command palette from a visible search control", () => {
    render(<WorkspaceHeader onResetLayout={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Search agents, tasks, and work/ }));

    expect(setPaletteOpen).toHaveBeenCalledWith(true);
    expect(screen.getByText("⌘/Ctrl K")).toBeInTheDocument();
  });
});
