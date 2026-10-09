import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
const issueCredit = vi.fn();
vi.mock("@/app/admin/_actions/billing", () => ({
  cancelSubscription: vi.fn(),
  pauseSubscription: vi.fn(),
  markInvoiceResolved: vi.fn(),
  issueCredit: (...args: unknown[]) => issueCredit(...args),
}));

import { BillingActionsPanel } from "../BillingActionsPanel";

beforeEach(() => {
  issueCredit.mockReset();
});

async function submitCredit(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Credit amount in cents"), "500");
  await user.type(screen.getByLabelText("Credit reason"), "goodwill");
  await user.click(screen.getByRole("button", { name: "Issue credit" }));
  const dialog = await screen.findByRole("alertdialog");
  await user.type(within(dialog).getByRole("textbox"), "u_target");
  await user.click(within(dialog).getByRole("button", { name: /^confirm /i }));
}

describe("Issue credit", () => {
  it("sends a new submission id for each deliberate credit", async () => {
    const user = userEvent.setup();
    issueCredit.mockResolvedValue({ ok: true, status: 200, data: { status: "credited" } });
    render(<BillingActionsPanel userId="u_target" email="a@b.co" />);

    await submitCredit(user);
    await submitCredit(user);

    const [first, second] = issueCredit.mock.calls.map((c) => c[3]);
    expect(first).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(second).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(first).not.toBe(second);
  });

  it("says so when Stripe deduplicated the credit", async () => {
    const user = userEvent.setup();
    issueCredit.mockResolvedValue({ ok: true, status: 200, data: { status: "duplicate" } });
    render(<BillingActionsPanel userId="u_target" email="a@b.co" />);

    await submitCredit(user);

    expect(await screen.findByText(/already issued.*no new credit/i)).toBeInTheDocument();
    expect(screen.queryByText("Credit issued.")).not.toBeInTheDocument();
  });
});
