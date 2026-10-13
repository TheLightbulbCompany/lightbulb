import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/app/admin/_actions/marketplace", () => ({
  approveListing: vi.fn(),
  rejectListing: vi.fn().mockResolvedValue({ ok: false, status: 409, error: "stale version" }),
}));

import { ModerationActions } from "../ModerationActions";

describe("ModerationActions reject", () => {
  it("shows a failed reject inside the still-open dialog, not behind its overlay", async () => {
    render(<ModerationActions listingId="l1" listingName="Acme" slug="acme" version={1} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.change(within(dialog).getByLabelText("Rejection notes"), { target: { value: "No." } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Reject listing" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("stale version");
  });
});
