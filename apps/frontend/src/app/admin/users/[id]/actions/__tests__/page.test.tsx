import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ActionsPage from "../page";

vi.mock("@clerk/nextjs/server", () => ({
  auth: async () => ({ getToken: async () => "test-token" }),
}));

vi.mock("@/app/admin/_lib/api", () => ({
  getActions: async () => ({ items: [], cursor: null }),
  getOverview: async () => ({
    identity: { email_addresses: [{ email_address: "owner@example.com" }] },
    org: { id: "org_123", name: "Acme" },
  }),
}));

describe("ActionsPage danger zone", () => {
  it("renders the danger zone card once", async () => {
    const ui = await ActionsPage({
      params: Promise.resolve({ id: "user_123" }),
      searchParams: Promise.resolve({}),
    });
    render(ui);

    expect(screen.getAllByRole("button", { name: "Wipe workspace" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Wipe account" })).toHaveLength(1);
    expect(screen.getAllByText("Danger zone")).toHaveLength(1);
  });
});
