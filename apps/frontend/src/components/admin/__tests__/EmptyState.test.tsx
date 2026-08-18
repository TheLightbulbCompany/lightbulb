import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { EmptyState } from "@/components/admin/EmptyState";

describe("EmptyState", () => {
  it("renders title and body", () => {
    render(<EmptyState title="No users yet" body="Day 1 is normal." />);
    expect(screen.getByText("No users yet")).toBeInTheDocument();
    expect(screen.getByText("Day 1 is normal.")).toBeInTheDocument();
  });

  it("renders link action when href is provided", () => {
    render(
      <EmptyState
        title="No users yet"
        body="Try inviting one."
        action={{ label: "Invite user", href: "/admin/users/invite" }}
      />,
    );
    const link = screen.getByRole("link", { name: "Invite user" });
    expect(link).toHaveAttribute("href", "/admin/users/invite");
  });

  it("renders button action when only onClick is provided", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <EmptyState
        title="No users yet"
        body="Reload to retry."
        action={{ label: "Retry", onClick }}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
