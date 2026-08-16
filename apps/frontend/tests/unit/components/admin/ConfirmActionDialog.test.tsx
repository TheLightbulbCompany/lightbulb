import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ConfirmActionDialog } from "@/components/admin/ConfirmActionDialog";

function renderDialog(props?: {
  onConfirm?: () => Promise<void> | void;
  confirmText?: string;
  actionLabel?: string;
}) {
  const onConfirm = props?.onConfirm ?? vi.fn();
  const utils = render(
    <ConfirmActionDialog
      confirmText={props?.confirmText ?? "DELETE"}
      actionLabel={props?.actionLabel ?? "Cancel subscription"}
      destructive
      onConfirm={onConfirm}
    >
      <button type="button">Open</button>
    </ConfirmActionDialog>,
  );
  return { ...utils, onConfirm };
}

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Open" }));
  // Radix portals the dialog content; wait for it to appear.
  await waitFor(() => {
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });
}

function getConfirmButton(actionLabel = "Cancel subscription"): HTMLButtonElement {
  return screen.getByRole("button", {
    name: `Confirm ${actionLabel}`,
  }) as HTMLButtonElement;
}

// Simulates Chrome autofill: it writes the input's DOM value through the
// native setter, bypassing React's tracked value, and never dispatches an
// `input`/`change` event -- so React's onChange (and thus `typed` state)
// never fires. This is the exact mechanism behind the reported bug.
function setNativeValueWithoutEvent(element: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )?.set;
  nativeSetter?.call(element, value);
}

describe("ConfirmActionDialog", () => {
  it("does not disable confirm on an empty field (validation happens at click time)", async () => {
    const user = userEvent.setup();
    renderDialog();
    await openDialog(user);

    expect(getConfirmButton()).toBeEnabled();
  });

  it("enables confirm button when correct text typed", async () => {
    const user = userEvent.setup();
    renderDialog({ confirmText: "DELETE" });
    await openDialog(user);

    const input = screen.getByLabelText("Type DELETE to confirm");
    await user.type(input, "DELETE");

    expect(getConfirmButton()).toBeEnabled();
  });

  it("calls onConfirm when correct text typed and clicked", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    renderDialog({ onConfirm, confirmText: "DELETE" });
    await openDialog(user);

    await user.type(screen.getByLabelText("Type DELETE to confirm"), "DELETE");
    await user.click(getConfirmButton());

    await waitFor(() => {
      expect(onConfirm).toHaveBeenCalledTimes(1);
    });
  });

  it("fires onConfirm when Chrome autofill sets the DOM value without a change event", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    renderDialog({ onConfirm, confirmText: "owner_abc123" });
    await openDialog(user);

    const input = screen.getByLabelText(
      "Type owner_abc123 to confirm",
    ) as HTMLInputElement;
    setNativeValueWithoutEvent(input, "owner_abc123");
    // React's `typed` state never saw this write -- the click handler must
    // still validate correctly by reading the DOM directly.
    expect(input.value).toBe("owner_abc123");

    await user.click(getConfirmButton());

    await waitFor(() => {
      expect(onConfirm).toHaveBeenCalledTimes(1);
    });
  });

  it("shows an inline error and does not confirm on an empty or wrong DOM value", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    renderDialog({ onConfirm, confirmText: "DELETE" });
    await openDialog(user);

    // Click with nothing typed at all.
    await user.click(getConfirmButton());
    expect(onConfirm).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(
        screen.getByText(/Type the exact id to confirm/i),
      ).toBeInTheDocument();
    });

    // A wrong autofilled value behaves the same way -- no silent dead click.
    const input = screen.getByLabelText("Type DELETE to confirm") as HTMLInputElement;
    setNativeValueWithoutEvent(input, "not-the-right-value");
    await user.click(getConfirmButton());
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("locks dialog after 3 wrong attempts", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    renderDialog({ onConfirm, confirmText: "DELETE" });
    await openDialog(user);

    const input = screen.getByLabelText("Type DELETE to confirm");
    await user.type(input, "WRONG");

    // Three clicks with mismatched text trigger the lockout.
    for (let i = 0; i < 3; i++) {
      await user.click(getConfirmButton());
    }

    expect(onConfirm).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(
        screen.getByText(/Locked\. Reload the page to try again\./i),
      ).toBeInTheDocument();
    });
    expect(getConfirmButton()).toBeDisabled();
  });

  it("aria-busy during async onConfirm", async () => {
    const user = userEvent.setup();
    let resolveConfirm: (() => void) | undefined;
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveConfirm = resolve;
        }),
    );
    renderDialog({ onConfirm, confirmText: "DELETE" });
    await openDialog(user);

    await user.type(screen.getByLabelText("Type DELETE to confirm"), "DELETE");
    const confirm = getConfirmButton();
    await user.click(confirm);

    await waitFor(() => {
      expect(getConfirmButton()).toHaveAttribute("aria-busy", "true");
    });
    expect(getConfirmButton()).toHaveTextContent(/Working/);
    // Busy still disables the button even though the empty-field guard
    // that used to do this implicitly is gone.
    expect(getConfirmButton()).toBeDisabled();

    resolveConfirm?.();
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
  });
});
