import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WipeAccountDialog } from "../WipeAccountDialog";

const mockPreview = vi.fn();
const mockExecute = vi.fn();

vi.mock("@/app/admin/_actions/wipe", () => ({
  previewAccountWipe: (...args: unknown[]) => mockPreview(...args),
  executeAccountWipe: (...args: unknown[]) => mockExecute(...args),
}));

const USER_ID = "user_test_123";
const EMAIL = "owner@example.com";

const PREVIEW_STEPS = [
  {
    system: "paperclip_company",
    action: "delete_company",
    status: "would_delete",
    detail: "company_abc",
    error: null,
  },
  {
    system: "ecs_service",
    action: "delete_service",
    status: "would_delete",
    detail: "openclaw-user_test_123",
    error: null,
  },
  {
    system: "clerk_user",
    action: "delete_user",
    status: "would_delete",
    detail: USER_ID,
    error: null,
  },
];

const RESULT_STEPS = [
  {
    system: "paperclip_company",
    action: "delete_company",
    status: "ok",
    detail: "deleted",
    error: null,
  },
  {
    system: "ecs_service",
    action: "delete_service",
    status: "failed",
    detail: "",
    error: "timeout contacting ECS",
  },
  {
    system: "clerk_user",
    action: "delete_user",
    status: "ok",
    detail: "deleted",
    error: null,
  },
];

async function openDialog() {
  const user = userEvent.setup();
  render(<WipeAccountDialog userId={USER_ID} email={EMAIL} />);
  await user.click(screen.getByRole("button", { name: /wipe account/i }));
  const dialog = await screen.findByRole("alertdialog");
  return { user, dialog };
}

beforeEach(() => {
  mockPreview.mockReset();
  mockExecute.mockReset();
  mockPreview.mockResolvedValue({
    ok: true,
    status: 200,
    data: { owner_id: USER_ID, dry_run: true, steps: PREVIEW_STEPS },
  });
  mockExecute.mockResolvedValue({
    ok: true,
    status: 200,
    data: { owner_id: USER_ID, dry_run: false, steps: RESULT_STEPS },
  });
});

describe("WipeAccountDialog", () => {
  it("fetches and renders the preview steps on open", async () => {
    const { dialog } = await openDialog();

    expect(mockPreview).toHaveBeenCalledWith(USER_ID);
    expect(within(dialog).getByText("delete_company")).toBeInTheDocument();
    expect(within(dialog).getByText("delete_service")).toBeInTheDocument();
    expect(within(dialog).getByText("delete_user")).toBeInTheDocument();
    expect(mockExecute).not.toHaveBeenCalled();
  });

  it("surfaces an org_wipe_unsupported preview error without a confirm input", async () => {
    mockPreview.mockResolvedValue({
      ok: false,
      status: 400,
      error: "org_wipe_unsupported",
    });
    const { dialog } = await openDialog();

    expect(within(dialog).getByText(/org_wipe_unsupported/i)).toBeInTheDocument();
    expect(within(dialog).queryByRole("textbox")).toBeNull();
  });

  it("keeps the wipe button disabled until the exact email is typed", async () => {
    const { user, dialog } = await openDialog();
    await within(dialog).findByText("delete_company");

    const wipeButton = within(dialog).getByRole("button", {
      name: /permanently wipe/i,
    });
    expect(wipeButton).toBeDisabled();

    const input = within(dialog).getByLabelText(new RegExp(`Type ${EMAIL}`, "i"));
    await user.type(input, "wrong@example.com");
    expect(wipeButton).toBeDisabled();

    await user.clear(input);
    await user.type(input, EMAIL);
    expect(wipeButton).toBeEnabled();
  });

  it("executes the wipe with the confirmed email and renders the result report", async () => {
    const { user, dialog } = await openDialog();
    await within(dialog).findByText("delete_company");

    const input = within(dialog).getByLabelText(new RegExp(`Type ${EMAIL}`, "i"));
    await user.type(input, EMAIL);

    const wipeButton = within(dialog).getByRole("button", {
      name: /permanently wipe/i,
    });
    await user.click(wipeButton);

    expect(await within(dialog).findByText("timeout contacting ECS")).toBeInTheDocument();
    expect(mockExecute).toHaveBeenCalledWith(USER_ID, EMAIL);
    expect(within(dialog).queryByRole("button", { name: /permanently wipe/i })).toBeNull();
  });

  it("surfaces an email_mismatch error from execute and does not render a result", async () => {
    mockExecute.mockResolvedValue({
      ok: false,
      status: 400,
      error: "email_mismatch",
    });
    const { user, dialog } = await openDialog();
    await within(dialog).findByText("delete_company");

    const input = within(dialog).getByLabelText(new RegExp(`Type ${EMAIL}`, "i"));
    await user.type(input, EMAIL);
    await user.click(within(dialog).getByRole("button", { name: /permanently wipe/i }));

    expect(await within(dialog).findByText(/email_mismatch/i)).toBeInTheDocument();
    expect(within(dialog).queryByText("timeout contacting ECS")).toBeNull();
  });

  it("disables the trigger when the account has no email on file", () => {
    render(<WipeAccountDialog userId={USER_ID} email={null} />);
    expect(screen.getByRole("button", { name: /wipe account/i })).toBeDisabled();
  });
});
