import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

const assign = vi.fn().mockResolvedValue(undefined);
const unassign = vi.fn().mockResolvedValue(undefined);
const disconnectAccount = vi.fn().mockResolvedValue(undefined);
const connectAnother = vi.fn().mockResolvedValue(undefined);

const googleState = {
  accounts: [
    { email: "admin@isol8.co", connected_at: "2026-08-01T00:00:00Z" },
    { email: "support@isol8.co", connected_at: "2026-08-02T00:00:00Z" },
  ],
  assignments: { vera: "support@isol8.co" } as Record<string, string>,
  isLoading: false,
  error: null as unknown,
  mutate: vi.fn(),
  disconnectAccount,
  assign,
  unassign,
  connectAnother,
};

vi.mock("@/hooks/useGoogleAccounts", () => ({
  useGoogleAccounts: () => googleState,
  ApiError: class ApiError extends Error {
    status = 0;
  },
}));

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({
    agents: [
      { id: "friday", name: "Friday" },
      { id: "vera", name: "Vera" },
    ],
    isLoading: false,
  }),
  agentDisplayName: (a: { name: string }) => a.name,
}));

vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

import { GoogleAccountsSection } from "../GoogleAccountsSection";

describe("GoogleAccountsSection", () => {
  beforeEach(() => vi.clearAllMocks());

  it("lists every connected account, not just the first", () => {
    render(<GoogleAccountsSection canManage />);
    // One disconnectable row per account — the emails also appear as <option>s
    // in every assignment select, so match on the row's own action.
    expect(
      screen.getByRole("button", { name: /disconnect admin@isol8\.co/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /disconnect support@isol8\.co/i }),
    ).toBeInTheDocument();
  });

  it("says how many agents act as each account so Disconnect isn't blind", () => {
    render(<GoogleAccountsSection canManage />);
    expect(screen.getByText(/no agent acts as this account/i)).toBeInTheDocument();
    expect(screen.getByText(/1 agent acts as this account/i)).toBeInTheDocument();
  });

  it("offers connecting another account", () => {
    render(<GoogleAccountsSection canManage />);
    fireEvent.click(screen.getByRole("button", { name: /connect another account/i }));
    expect(connectAnother).toHaveBeenCalled();
  });

  it("binds an account to an agent, and reverting picks Default", () => {
    render(<GoogleAccountsSection canManage />);
    const fridaySelect = screen.getByLabelText("Friday") as HTMLSelectElement;
    expect(fridaySelect.value).toBe("__default__");

    fireEvent.change(fridaySelect, { target: { value: "admin@isol8.co" } });
    expect(assign).toHaveBeenCalledWith("admin@isol8.co", "friday");

    const veraSelect = screen.getByLabelText("Vera") as HTMLSelectElement;
    expect(veraSelect.value).toBe("support@isol8.co");
    fireEvent.change(veraSelect, { target: { value: "__default__" } });
    expect(unassign).toHaveBeenCalledWith("vera");
  });

  it("states that an assignment governs every run, not one task", () => {
    render(<GoogleAccountsSection canManage />);
    expect(
      screen.getByText(/for every Google action, on every run/i),
    ).toBeInTheDocument();
  });

  it("disables every mutation for non-admins", () => {
    render(<GoogleAccountsSection canManage={false} />);
    expect(
      screen.getByRole("button", { name: /disconnect admin@isol8\.co/i }),
    ).toBeDisabled();
    expect(screen.getByLabelText("Friday")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /connect another account/i }),
    ).toBeDisabled();
  });
});
