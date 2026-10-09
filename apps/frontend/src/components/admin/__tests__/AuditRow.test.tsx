import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AuditRow, type AuditEntry } from "../AuditRow";

const entry = (target_user_id: string): AuditEntry => ({
  admin_user_id: "user_admin",
  timestamp_action_id: "2026-10-01T00:00:00Z#01",
  target_user_id,
  action: "marketplace.takedown",
  result: "success",
  http_status: 200,
  elapsed_ms: 5,
});

describe("AuditRow", () => {
  it("links a real target to its user page", () => {
    render(<AuditRow entry={entry("org_123")} />);
    expect(screen.getByRole("link", { name: "org_123" })).toHaveAttribute("href", "/admin/users/org_123");
  });

  it("renders a sentinel target as text, never a broken user link", () => {
    render(<AuditRow entry={entry("__marketplace__")} />);
    expect(screen.getByText("__marketplace__")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
