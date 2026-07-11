import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TeamBillingCard } from "../TeamBillingCard";

describe("TeamBillingCard", () => {
  it("renders N seats × $price = total for an admin", () => {
    render(<TeamBillingCard seatCount={4} seatPriceUsd={20} ownerRole="admin" />);
    expect(screen.getByText(/4 seats/i)).toBeInTheDocument();
    expect(screen.getByText(/\$20/)).toBeInTheDocument();
    expect(screen.getByText(/\$80/)).toBeInTheDocument();
  });

  it("calls onSubscribe when an unsubscribed admin clicks subscribe", () => {
    const onSubscribe = vi.fn().mockResolvedValue(undefined);
    render(
      <TeamBillingCard seatCount={2} seatPriceUsd={20} ownerRole="admin" isSubscribed={false} onSubscribe={onSubscribe} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /subscribe your team/i }));
    expect(onSubscribe).toHaveBeenCalled();
  });

  it("shows the ask-admin message and no CTA for a member", () => {
    render(<TeamBillingCard seatCount={3} seatPriceUsd={20} ownerRole="member" />);
    expect(screen.getByText(/ask your org admin/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /subscribe/i })).toBeNull();
  });

  it("shows Manage for a subscribed admin", () => {
    const onManage = vi.fn().mockResolvedValue(undefined);
    render(
      <TeamBillingCard seatCount={5} seatPriceUsd={20} ownerRole="admin" isSubscribed onManage={onManage} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /manage billing/i }));
    expect(onManage).toHaveBeenCalled();
  });

  it("shows a neutral loading placeholder instead of '0 seats × $0' when seatCount is null", () => {
    render(<TeamBillingCard seatCount={null} seatPriceUsd={20} ownerRole="admin" />);
    expect(screen.getByText(/loading team size/i)).toBeInTheDocument();
    expect(screen.queryByText(/0 seats/i)).toBeNull();
    expect(screen.queryByText(/\$0\/mo/)).toBeNull();
  });
});
