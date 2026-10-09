import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
const takedownListing = vi.fn();
vi.mock("@/app/admin/_actions/marketplace", () => ({ takedownListing: (...a: unknown[]) => takedownListing(...a) }));

import { TakedownButton } from "../TakedownButton";

describe("TakedownButton", () => {
  it("tells the admin a typed phrase is wrong before the lockout, and never posts", () => {
    render(<TakedownButton listingId="l1" listingName="Acme" slug="acme" />);
    fireEvent.click(screen.getByRole("button", { name: "Take down listing" }));
    fireEvent.change(screen.getByLabelText(/takedown basis/i), { target: { value: "Policy violation here." } });
    fireEvent.change(screen.getByLabelText("Type takedown acme to confirm"), { target: { value: "takedown acm" } });
    const submit = screen.getAllByRole("button", { name: "Take down listing" }).at(-1)!;
    fireEvent.click(submit);
    expect(screen.getByText(/doesn.t match\. 2 attempts left/)).toBeInTheDocument();
    expect(takedownListing).not.toHaveBeenCalled();
  });
});
