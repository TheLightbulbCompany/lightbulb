import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WipeWorkspaceDialog } from "../WipeWorkspaceDialog";

const mockPreview = vi.fn();
const mockExecute = vi.fn();

vi.mock("@/app/admin/_actions/wipe", () => ({
  previewWorkspaceWipe: (...args: unknown[]) => mockPreview(...args),
  executeWorkspaceWipe: (...args: unknown[]) => mockExecute(...args),
}));

const ORG_ID = "org_test_123";

function preview(fingerprint: string) {
  return {
    ok: true,
    status: 200,
    data: {
      owner_id: ORG_ID,
      dry_run: true,
      fingerprint,
      members: [
        { user_id: "user_a", email: "a@example.com", action: "delete_account", other_orgs: [] },
        { user_id: "user_b", email: "b@example.com", action: "remove_membership", other_orgs: ["org_other"] },
      ],
      steps: [
        { system: "agentcore_runtime", action: "delete_runtime", status: "would_delete", detail: "rt-1", error: null },
        { system: "stripe_customer", action: "delete_customer", status: "skipped", detail: "PRESERVED by default", error: null },
      ],
    },
  };
}

async function openDialog() {
  const user = userEvent.setup();
  render(<WipeWorkspaceDialog orgId={ORG_ID} />);
  await user.click(screen.getByRole("button", { name: /wipe workspace/i }));
  const dialog = await screen.findByRole("alertdialog");
  await within(dialog).findByText("delete_runtime");
  return { user, dialog };
}

beforeEach(() => {
  mockPreview.mockReset();
  mockExecute.mockReset();
  mockPreview.mockResolvedValue(preview("fp1"));
  mockExecute.mockResolvedValue({
    ok: true,
    status: 200,
    data: {
      steps: [{ system: "agentcore_runtime", action: "delete_runtime", status: "ok", detail: "deleted", error: null }],
    },
  });
});

describe("WipeWorkspaceDialog", () => {
  it("previews on open with default layers and billing off, and lists members", async () => {
    const { dialog } = await openDialog();
    expect(mockPreview).toHaveBeenCalledWith(ORG_ID, {
      layers: ["runtime", "paperclip", "messaging", "secrets", "identity"],
      includeBilling: false,
      deleteSharedMembers: false,
    });
    expect(within(dialog).getByText("a@example.com")).toBeInTheDocument();
    expect(within(dialog).getByText(/org_other/)).toBeInTheDocument();
  });

  it("keeps execute disabled until the exact org id is typed", async () => {
    const { user, dialog } = await openDialog();
    const button = within(dialog).getByRole("button", { name: /permanently wipe/i });
    expect(button).toBeDisabled();
    const input = within(dialog).getByLabelText(new RegExp(`Type ${ORG_ID} to confirm`, "i"));
    await user.type(input, `${ORG_ID} `);
    expect(button).toBeDisabled();
    await user.clear(input);
    await user.type(input, ORG_ID);
    expect(button).toBeEnabled();
  });

  it("requires DELETE-BILLING when billing is included", async () => {
    const { user, dialog } = await openDialog();
    await user.click(within(dialog).getByLabelText(/billing/i, { selector: "input[type=checkbox]" }));
    await waitFor(() => expect(mockPreview).toHaveBeenLastCalledWith(ORG_ID, expect.objectContaining({ includeBilling: true, layers: expect.arrayContaining(["billing"]) })));
    await user.type(within(dialog).getByLabelText(new RegExp(`Type ${ORG_ID} to confirm`, "i")), ORG_ID);
    const button = within(dialog).getByRole("button", { name: /permanently wipe/i });
    expect(button).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Type DELETE-BILLING/i), "DELETE-BILLING");
    expect(button).toBeEnabled();
    await user.click(button);
    expect(mockExecute).toHaveBeenCalledWith(
      ORG_ID,
      expect.objectContaining({ includeBilling: true, confirmBilling: "DELETE-BILLING" }),
    );
  });

  it("re-previews on a layer toggle and executes with the new fingerprint", async () => {
    mockPreview.mockResolvedValueOnce(preview("fp1")).mockResolvedValueOnce(preview("fp2"));
    const { user, dialog } = await openDialog();
    await user.click(within(dialog).getByLabelText("messaging"));
    await waitFor(() => expect(mockPreview).toHaveBeenCalledTimes(2));
    expect(mockPreview).toHaveBeenLastCalledWith(
      ORG_ID,
      expect.objectContaining({ layers: ["runtime", "paperclip", "secrets", "identity"] }),
    );
    await within(dialog).findByText("delete_runtime");
    await user.type(within(dialog).getByLabelText(new RegExp(`Type ${ORG_ID} to confirm`, "i")), ORG_ID);
    await user.click(within(dialog).getByRole("button", { name: /permanently wipe/i }));
    expect(mockExecute).toHaveBeenCalledWith(ORG_ID, {
      confirmOrg: ORG_ID,
      previewFingerprint: "fp2",
      layers: ["runtime", "paperclip", "secrets", "identity"],
      includeBilling: false,
      confirmBilling: undefined,
      deleteSharedMembers: false,
    });
  });

  it("shows a re-run message when a step failed", async () => {
    mockExecute.mockResolvedValue({
      ok: true,
      status: 200,
      data: {
        steps: [{ system: "clerk_org", action: "delete_org", status: "failed", detail: "", error: "clerk 500" }],
      },
    });
    const { user, dialog } = await openDialog();
    await user.type(within(dialog).getByLabelText(new RegExp(`Type ${ORG_ID} to confirm`, "i")), ORG_ID);
    await user.click(within(dialog).getByRole("button", { name: /permanently wipe/i }));
    expect(await within(dialog).findByText("clerk 500")).toBeInTheDocument();
    expect(within(dialog).getByText(/partial.*re-run/i)).toBeInTheDocument();
  });

  it("re-previews on stale_preview and says so", async () => {
    mockExecute.mockResolvedValueOnce({
      ok: false,
      status: 409,
      error: "stale_preview: fingerprint drift",
      code: "stale_preview",
    });
    const { user, dialog } = await openDialog();
    await user.type(within(dialog).getByLabelText(new RegExp(`Type ${ORG_ID} to confirm`, "i")), ORG_ID);
    await user.click(within(dialog).getByRole("button", { name: /permanently wipe/i }));
    expect(await within(dialog).findByText(/workspace changed since preview — re-previewed/i)).toBeInTheDocument();
    expect(mockPreview).toHaveBeenCalledTimes(2);
  });
});
