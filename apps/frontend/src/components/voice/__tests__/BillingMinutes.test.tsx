// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const usage = vi.hoisted(() => ({ paidAllowance: false }));
vi.mock("../data", () => ({
  useUsage: () => ({ data: { used: 112, included: 1500, overageRate: 0.35, runOut: "keep", ...usage } }),
  useAgentConfig: () => ({ data: { runOut: "keep" }, save: vi.fn() }),
}));
vi.mock("../VoicePlanCard", () => ({ VoicePlanCard: () => null }));
vi.mock("@/components/settings/DailySummaryCard", () => ({ DailySummaryCard: () => null }));
vi.mock("@/components/desktop/DesktopUpdater", () => ({ DesktopUpdater: () => null }));
vi.mock("@/lib/desktopNotify", () => ({ isDesktopApp: () => false }));

import { BillingMinutes } from "../VoiceSettings";

beforeEach(() => { usage.paidAllowance = false; });

it("shows historical usage without claiming an included allowance before paid access", () => {
  render(<BillingMinutes />);
  expect(screen.getByText(/voice min used this month/)).toHaveTextContent("112 voice min used this month");
  expect(screen.getByText(/Included minutes apply while/)).toBeInTheDocument();
  expect(screen.queryByRole("meter")).not.toBeInTheDocument();
  expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  expect(screen.queryByText(/Your included allowance is/)).not.toBeInTheDocument();
});

it("shows the allowance and run-out choices when payment covers the current period", () => {
  usage.paidAllowance = true;
  render(<BillingMinutes />);
  expect(screen.getByRole("meter", { name: "Minutes used" })).toHaveAttribute("aria-valuenow", "112");
  expect(screen.getByText(/Your included allowance is/)).toHaveTextContent("1,500 minutes");
  expect(screen.getByRole("radio", { name: /Keep answering/ })).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: /Send calls to my phone/ })).toBeInTheDocument();
});
