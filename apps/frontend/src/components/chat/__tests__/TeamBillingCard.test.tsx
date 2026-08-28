import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TeamBillingCard } from "../TeamBillingCard";

describe("TeamBillingCard", () => {
  it("renders N seats × $price = total for an admin", () => {
    render(<TeamBillingCard seatCount={4} seatPriceUsd={20} basePriceUsd={50} ownerRole="admin" />);
    expect(screen.getByText(/4 seats/i)).toBeInTheDocument();
    expect(screen.getByText(/\$20/)).toBeInTheDocument();
    expect(screen.getByText(/\$130/)).toBeInTheDocument();
  });

  it("prices base plus seats, not seats alone", () => {
    render(<TeamBillingCard seatCount={1} seatPriceUsd={20} basePriceUsd={50} ownerRole="admin" />);
    expect(screen.getByText(/\$70\/mo/)).toBeInTheDocument();
  });

  it("counts an invited-but-unjoined person as a billable seat", () => {
    // seatCount already includes pending invitations — 1 admin + 3 invites.
    render(<TeamBillingCard seatCount={4} seatPriceUsd={20} basePriceUsd={50} ownerRole="admin" />);
    expect(screen.getByText(/\$130\/mo/)).toBeInTheDocument();
  });

  it("falls back to seats-only when the base price is absent", () => {
    render(<TeamBillingCard seatCount={2} seatPriceUsd={20} basePriceUsd={null} ownerRole="admin" />);
    expect(screen.getByText(/\$40\/mo/)).toBeInTheDocument();
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

  // The card is mounted on /chat as well as /settings, so it must carry its
  // own styling — the `.settings-*` classes it used to rely on live in a
  // route-scoped stylesheet the chat route never loads, which is what made the
  // org billing gate paint as raw unstyled text.
  it("carries its own card chrome and a real styled CTA (no settings-scoped classes)", () => {
    const { container } = render(
      <TeamBillingCard seatCount={4} seatPriceUsd={20} ownerRole="admin" onSubscribe={vi.fn()} />,
    );
    expect(container.querySelector('[class*="settings-"]')).toBeNull();

    const card = screen.getByTestId("team-billing-card");
    expect(card.className).toMatch(/bg-cream(?![-\w])/);
    expect(card.className).toMatch(/\bborder\b/);
    expect(card.className).toMatch(/rounded-2xl/);

    const cta = screen.getByRole("button", { name: /subscribe your team/i });
    expect(cta.className).toMatch(/bg-\[var\(--midnight\)\]/);
    expect(cta.className).toMatch(/rounded-full/);
  });

  // `action.admin_only` is a wire field on the gate payload, not an assumption
  // the card gets to make.
  it("honors adminOnly=false by showing the CTA to a member", () => {
    const onSubscribe = vi.fn().mockResolvedValue(undefined);
    render(
      <TeamBillingCard
        seatCount={3}
        seatPriceUsd={20}
        ownerRole="member"
        adminOnly={false}
        onSubscribe={onSubscribe}
      />,
    );
    expect(screen.getByRole("button", { name: /subscribe your team/i })).toBeInTheDocument();
    expect(screen.queryByText(/ask your org admin/i)).toBeNull();
  });

  it("keeps the CTA hidden for a member when adminOnly is explicitly true", () => {
    render(<TeamBillingCard seatCount={3} seatPriceUsd={20} ownerRole="member" adminOnly />);
    expect(screen.getByText(/ask your org admin/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /subscribe/i })).toBeNull();
  });

  it("shows a neutral loading placeholder instead of '0 seats × $0' when seatCount is null", () => {
    render(<TeamBillingCard seatCount={null} seatPriceUsd={20} ownerRole="admin" />);
    expect(screen.getByText(/loading team size/i)).toBeInTheDocument();
    expect(screen.queryByText(/0 seats/i)).toBeNull();
    expect(screen.queryByText(/\$0\/mo/)).toBeNull();
  });
});
