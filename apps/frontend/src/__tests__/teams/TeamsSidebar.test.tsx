// Smoke test for the /teams sidebar: confirms all 13 panel links render
// and the back-to-chat anchor points at /chat.
//
// The global tests/setup.ts mocks `next/navigation` (returning `usePathname`
// → "/"), but we override that here so the active-link styling logic exercises
// the /teams/dashboard branch.
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  usePathname: () => "/teams/dashboard",
}));

import { TeamsSidebar } from "@/components/teams/TeamsSidebar";

describe("TeamsSidebar", () => {
  it("renders all 13 panel links", () => {
    render(<TeamsSidebar />);
    [
      "Dashboard",
      "Agents",
      "Inbox",
      "Approvals",
      "Issues",
      "Routines",
      "Goals",
      "Projects",
      "Activity",
      "Costs",
      "Skills",
      "Members",
      "Settings",
    ].forEach((label) => {
      expect(screen.getByText(label)).toBeInTheDocument();
    });
  });

  it("Back to chat link points to /chat", () => {
    render(<TeamsSidebar />);
    const link = screen.getByText("Back to chat");
    expect(link.closest("a")).toHaveAttribute("href", "/chat");
  });
});
