import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
const issueCredit = vi.fn();
const resumeSubscription = vi.fn();
vi.mock("@/app/admin/_actions/billing", () => ({
  cancelSubscription: vi.fn(),
  pauseSubscription: vi.fn(),
  resumeSubscription: (...args: unknown[]) => resumeSubscription(...args),
  markInvoiceResolved: vi.fn(),
  issueCredit: (...args: unknown[]) => issueCredit(...args),
}));

import { BillingActionsPanel } from "../BillingActionsPanel";

beforeEach(() => {
  issueCredit.mockReset();
});

async function submitCredit(user: ReturnType<typeof userEvent.setup>, amount = "500", reason = "goodwill") {
  await user.clear(screen.getByLabelText("Credit amount in cents"));
  await user.type(screen.getByLabelText("Credit amount in cents"), amount);
  await user.clear(screen.getByLabelText("Credit reason"));
  await user.type(screen.getByLabelText("Credit reason"), reason);
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

  it("a retry after an ambiguous failure reuses the submission id; a changed amount or reason gets a new one", async () => {
    const user = userEvent.setup();
    // No response (network) and a 502: Stripe may already have created the credit.
    issueCredit.mockResolvedValueOnce({ ok: false, status: 0, error: "network_error" });
    issueCredit.mockResolvedValueOnce({ ok: false, status: 502, error: "stripe_credit_failed" });
    issueCredit.mockResolvedValue({ ok: false, status: 0, error: "network_error" });
    render(<BillingActionsPanel userId="u_target" email="a@b.co" />);

    await submitCredit(user);
    await submitCredit(user);
    await submitCredit(user, "500", "goodwill again");
    await submitCredit(user, "700", "goodwill again");

    const ids = issueCredit.mock.calls.map((c) => c[3]);
    expect(ids[1]).toBe(ids[0]);
    expect(ids[2]).not.toBe(ids[1]);
    expect(ids[3]).not.toBe(ids[2]);
  });

  it("a definite 4xx refusal ends the submission; the next one gets a new id", async () => {
    const user = userEvent.setup();
    issueCredit.mockResolvedValueOnce({ ok: false, status: 409, error: "no customer" });
    issueCredit.mockResolvedValue({ ok: true, status: 200, data: { status: "credited" } });
    render(<BillingActionsPanel userId="u_target" email="a@b.co" />);

    await submitCredit(user);
    await submitCredit(user);

    const [first, second] = issueCredit.mock.calls.map((c) => c[3]);
    expect(second).not.toBe(first);
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

describe("Resume subscription", () => {
  it("resumes behind the same typed confirmation as Pause", async () => {
    const user = userEvent.setup();
    resumeSubscription.mockResolvedValue({ ok: true, status: 200, data: { status: "resumed" } });
    render(<BillingActionsPanel userId="u_target" email="a@b.co" />);

    await user.click(screen.getByRole("button", { name: "Resume subscription" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByRole("textbox"), "a@b.co");
    await user.click(within(dialog).getByRole("button", { name: /^confirm /i }));

    expect(resumeSubscription).toHaveBeenCalledWith("u_target");
    expect(await screen.findByText("Resume subscription succeeded.")).toBeInTheDocument();
  });
});
