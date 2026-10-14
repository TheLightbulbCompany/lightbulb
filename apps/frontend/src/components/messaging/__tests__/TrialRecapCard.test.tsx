import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ recap: null as unknown, role: "org:admin", startCheckout: vi.fn() }));
vi.mock("swr", () => ({ default: () => ({ data: state.recap }) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn() }) }));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: { role: state.role } }) }));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    startCheckout: state.startCheckout,
    account: { workspace_price_usd: 50, included_members: 5 },
  }),
}));

import { TrialRecapCard, recapLine } from "../TrialRecapCard";

const recap = { show: true, trial_end: 1_800_000_000, handoffs: 4, approvals: 3, agents: 2, people: 3, top_item: "Send invoices" };

describe("TrialRecapCard", () => {
  beforeEach(() => {
    window.localStorage.clear();
    state.role = "org:admin";
    state.startCheckout = vi.fn().mockResolvedValue(undefined);
  });

  it("shows only counts above zero", () => {
    expect(recapLine({ ...recap, show: true })).toBe("4 handoffs · 3 approvals · 2 agents did work for 3 people");
    expect(recapLine({ ...recap, show: true, handoffs: 0, approvals: 1, agents: 0 })).toBe("1 approval");
  });

  it("lets an admin keep the team, and hides for three days on Not now", async () => {
    state.recap = recap;
    const user = userEvent.setup();
    render(<TrialRecapCard />);
    expect(screen.getByText("Send invoices")).toBeInTheDocument();
    expect(screen.getByText("Keep going for $50 a month. People are free up to 5.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Keep the team" }));
    expect(state.startCheckout).toHaveBeenCalledWith("workspace");
    await user.click(screen.getByRole("button", { name: "Not now" }));
    expect(screen.queryByTestId("trial-recap")).toBeNull();
  });

  it("points a member at an admin, and offers help when nothing finished", () => {
    state.role = "org:member";
    state.recap = { ...recap, handoffs: 0, approvals: 0 };
    render(<TrialRecapCard />);
    expect(screen.queryByRole("button", { name: "Keep the team" })).toBeNull();
    expect(screen.getByText("Ask an admin to keep the team.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Book 20 minutes" })).toBeInTheDocument();
  });

  it("renders nothing outside the window", () => {
    state.recap = { show: false };
    render(<TrialRecapCard />);
    expect(screen.queryByTestId("trial-recap")).toBeNull();
  });
});
