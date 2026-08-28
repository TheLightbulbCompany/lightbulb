import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const joinWaitlist = vi.fn();
const fetchLaunchInfo = vi.fn();
vi.mock("@/lib/launch", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/launch")>()),
  joinWaitlist: (...args: unknown[]) => joinWaitlist(...args),
  fetchLaunchInfo: () => fetchLaunchInfo(),
}));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const useLaunchRef = vi.fn<() => string | null>(() => null);
vi.mock("../useLaunchRef", () => ({ useLaunchRef: () => useLaunchRef() }));

import { WaitlistForm } from "../WaitlistForm";

const info = { phase: "waitlist" as const, drop_at: "d", competition_start_at: "s", competition_end_at: "e", pot_cents: 100000, entrants: 0, waitlist_count: 12, comp_slots: 220 };

describe("WaitlistForm", () => {
  beforeEach(() => { joinWaitlist.mockReset(); fetchLaunchInfo.mockReset(); useLaunchRef.mockReturnValue(null); });

  it("submits the email with the stored ref and shows position + referral link", async () => {
    fetchLaunchInfo.mockResolvedValue(info);
    useLaunchRef.mockReturnValue("K7QX2MPP");
    joinWaitlist.mockResolvedValue({ position: 47, referral_code: "P9AB3CDE", in_comp_window: true });
    render(<WaitlistForm />);
    fireEvent.change(await screen.findByLabelText(/email/i), { target: { value: "me@x.com" } });
    fireEvent.click(screen.getByRole("button", { name: /join/i }));
    await waitFor(() => expect(joinWaitlist).toHaveBeenCalledWith("me@x.com", "K7QX2MPP"));
    expect(await screen.findByText(/#47/)).toBeInTheDocument();
    expect(screen.getByText(/first 220/i)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/\/\?ref=P9AB3CDE$/)).toBeInTheDocument();
  });

  it("tells a late joiner they are past the comp window", async () => {
    fetchLaunchInfo.mockResolvedValue(info);
    joinWaitlist.mockResolvedValue({ position: 305, referral_code: "X", in_comp_window: false });
    render(<WaitlistForm />);
    fireEvent.change(await screen.findByLabelText(/email/i), { target: { value: "me@x.com" } });
    fireEvent.click(screen.getByRole("button", { name: /join/i }));
    expect(await screen.findByText(/#305/)).toBeInTheDocument();
    expect(screen.queryByText(/first 220/i)).not.toBeInTheDocument();
  });

  it("surfaces a backend rejection", async () => {
    fetchLaunchInfo.mockResolvedValue(info);
    joinWaitlist.mockRejectedValue(new Error("invalid_email"));
    render(<WaitlistForm />);
    fireEvent.change(await screen.findByLabelText(/email/i), { target: { value: "nope@" } });
    fireEvent.click(screen.getByRole("button", { name: /join/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/valid email/i);
  });

  it("renders nothing once the waitlist is closed or the feature is off", async () => {
    fetchLaunchInfo.mockResolvedValue({ ...info, phase: "activation" });
    const { container, rerender } = render(<WaitlistForm />);
    await waitFor(() => expect(fetchLaunchInfo).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    fetchLaunchInfo.mockResolvedValue(null);
    rerender(<WaitlistForm />);
    expect(container).toBeEmptyDOMElement();
  });
});
