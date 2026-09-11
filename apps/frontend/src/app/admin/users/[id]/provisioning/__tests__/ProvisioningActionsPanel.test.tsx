import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockRefresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: mockRefresh }) }));

const mockRetryProvision = vi.fn();
vi.mock("@/app/admin/_actions/provisioning", () => ({
  retryProvision: (...args: unknown[]) => mockRetryProvision(...args),
}));

import { ProvisioningActionsPanel } from "../ProvisioningActionsPanel";

const USER_ID = "user_test_123";

beforeEach(() => {
  mockRefresh.mockReset();
  mockRetryProvision.mockReset();
  mockRetryProvision.mockResolvedValue({ ok: true, status: 200, data: { ok: true } });
});

describe("ProvisioningActionsPanel", () => {
  it("renders the provisioning retry action", () => {
    render(
      <ProvisioningActionsPanel
        userId={USER_ID}
        containerStatus="running"
      />,
    );

    expect(screen.getByRole("heading", { name: /retry provision/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^retry provision$/i })).toBeInTheDocument();
  });

  it("opens a typed-confirm dialog for retry and calls retryProvision on confirm", async () => {
    const user = userEvent.setup();
    render(
      <ProvisioningActionsPanel
        userId={USER_ID}
        containerStatus="running"
      />,
    );

    await user.click(screen.getByRole("button", { name: /^retry provision$/i }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(USER_ID)).toBeInTheDocument();

    const input = within(dialog).getByLabelText(new RegExp(`Type ${USER_ID}`, "i"));
    await user.type(input, USER_ID);
    await user.click(within(dialog).getByRole("button", { name: /confirm retry provision/i }));

    expect(mockRetryProvision).toHaveBeenCalledWith(USER_ID);
    expect(await screen.findByText(/retry provision succeeded/i)).toBeInTheDocument();
    expect(mockRefresh).toHaveBeenCalled();
  });

  it("does not call retryProvision when the typed confirm does not match", async () => {
    const user = userEvent.setup();
    render(
      <ProvisioningActionsPanel
        userId={USER_ID}
        containerStatus="running"
      />,
    );

    await user.click(screen.getByRole("button", { name: /^retry provision$/i }));
    const dialog = await screen.findByRole("alertdialog");

    const input = within(dialog).getByLabelText(new RegExp(`Type ${USER_ID}`, "i"));
    await user.type(input, "wrong_id");
    await user.click(within(dialog).getByRole("button", { name: /confirm retry provision/i }));

    expect(mockRetryProvision).not.toHaveBeenCalled();
  });

  it("renders an error banner when an action fails", async () => {
    mockRetryProvision.mockResolvedValue({
      ok: false,
      status: 409,
      error: "provisioning_fresh_claim",
    });
    const user = userEvent.setup();
    render(
      <ProvisioningActionsPanel
        userId={USER_ID}
        containerStatus="provisioning"
      />,
    );

    await user.click(screen.getByRole("button", { name: /^retry provision$/i }));
    const dialog = await screen.findByRole("alertdialog");
    const input = within(dialog).getByLabelText(new RegExp(`Type ${USER_ID}`, "i"));
    await user.type(input, USER_ID);
    await user.click(within(dialog).getByRole("button", { name: /confirm retry provision/i }));

    expect(await screen.findByText(/provisioning_fresh_claim/i)).toBeInTheDocument();
  });
});
