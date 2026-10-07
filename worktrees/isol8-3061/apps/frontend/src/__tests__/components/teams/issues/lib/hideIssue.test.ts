// Hiding a task is effectively irreversible from the UI: Paperclip's issue
// reads apply `hidden_at IS NULL` unconditionally and `IssueFilters` has no
// include-hidden escape, so nothing can list a hidden task to restore it. The
// Undo toast is the only route back — these guard that it is actually offered,
// that it sends the clearing patch, and that it is never offered for a hide
// that failed.
import { describe, it, expect, vi, beforeEach } from "vitest";

const notifySuccess = vi.fn();
const notifyApiError = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifySuccess: (...args: unknown[]) => notifySuccess(...args),
  notifyApiError: (...args: unknown[]) => notifyApiError(...args),
}));

import { hideIssueWithUndo } from "@/components/teams/issues/lib/hideIssue";

beforeEach(() => {
  notifySuccess.mockClear();
  notifyApiError.mockClear();
});

describe("hideIssueWithUndo", () => {
  it("stamps hiddenAt, then offers an Undo that clears it", async () => {
    const update = vi.fn().mockResolvedValue(undefined);
    await hideIssueWithUndo("i1", update);

    expect(update).toHaveBeenCalledTimes(1);
    const [id, patch] = update.mock.calls[0];
    expect(id).toBe("i1");
    expect(typeof patch.hiddenAt).toBe("string");

    const opts = notifySuccess.mock.calls[0][1];
    expect(opts.action.label).toBe("Undo");
    opts.action.onClick();
    expect(update).toHaveBeenLastCalledWith("i1", { hiddenAt: null });
  });

  it("waits for the write before offering Undo", async () => {
    let settle: () => void = () => {};
    const update = vi.fn(
      () => new Promise<void>((resolve) => (settle = resolve)),
    );
    const done = hideIssueWithUndo("i1", update);
    // The PATCH is still in flight — a toast here would promise an undo for a
    // hide that has not happened.
    expect(notifySuccess).not.toHaveBeenCalled();
    settle();
    await done;
    expect(notifySuccess).toHaveBeenCalledTimes(1);
  });

  it("reports the failure and offers no Undo when the hide rejects", async () => {
    const update = vi.fn().mockRejectedValue(new Error("boom"));
    const onHidden = vi.fn();
    await hideIssueWithUndo("i1", update, onHidden);

    expect(notifyApiError).toHaveBeenCalledTimes(1);
    expect(notifySuccess).not.toHaveBeenCalled();
    // The task is still visible, so the caller must not navigate away from it.
    expect(onHidden).not.toHaveBeenCalled();
  });

  it("runs onHidden only after a successful hide", async () => {
    const update = vi.fn().mockResolvedValue(undefined);
    const onHidden = vi.fn();
    await hideIssueWithUndo("i1", update, onHidden);
    expect(onHidden).toHaveBeenCalledTimes(1);
  });
});
