import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { TeamBillingCard } from "../TeamBillingCard";

describe("TeamBillingCard", () => {
  it("renders one workspace price covering five people", () => {
    render(<TeamBillingCard workspacePriceUsd={50} ownerRole="admin" />);
    expect(screen.getByText(/\$50\/mo/)).toBeInTheDocument();
    expect(screen.getByText(/up to 5 people/i)).toBeInTheDocument();
    expect(screen.getByText(/from the 6th adds a seat/i)).toBeInTheDocument();
  });

  it("discloses the two current inference choices beside the workspace CTA", () => {
    render(<TeamBillingCard workspacePriceUsd={50} ownerRole="admin" />);
    expect(screen.getByText(/separate ChatGPT subscription/i)).toBeInTheDocument();
    // Each path is named, not just described.
    for (const name of ["OpenRouter managed", "Sign in with ChatGPT"]) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
    expect(screen.queryByText(/bring your own api key/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /subscribe workspace/i })).toBeInTheDocument();
  });

  it("does not fabricate a price while the billing account is loading", () => {
    render(<TeamBillingCard workspacePriceUsd={undefined} ownerRole="admin" />);
    expect(screen.getByText(/loading workspace price/i)).toBeInTheDocument();
    expect(screen.queryByText(/\$50\/mo/)).toBeNull();
  });

  it("calls onSubscribe when an unsubscribed admin clicks subscribe", () => {
    const onSubscribe = vi.fn().mockResolvedValue(undefined);
    render(
      <TeamBillingCard workspacePriceUsd={50} ownerRole="admin" isSubscribed={false} onSubscribe={onSubscribe} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /subscribe workspace/i }));
    expect(onSubscribe).toHaveBeenCalled();
  });

  it("re-enables Subscribe after a same-plan/update response stays on this page", async () => {
    const onSubscribe = vi.fn().mockResolvedValue(undefined);
    render(
      <TeamBillingCard workspacePriceUsd={50} ownerRole="admin" isSubscribed={false} onSubscribe={onSubscribe} />,
    );
    const button = screen.getByRole("button", { name: /subscribe workspace/i });
    fireEvent.click(button);
    await waitFor(() => expect(button).toBeEnabled());
    expect(onSubscribe).toHaveBeenCalledOnce();
  });

  it("shows the ask-admin message and no CTA for a member", () => {
    render(<TeamBillingCard workspacePriceUsd={50} ownerRole="member" />);
    expect(screen.getByText(/ask your workspace admin/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /subscribe/i })).toBeNull();
  });

  it("shows Manage for a subscribed admin", () => {
    const onManage = vi.fn().mockResolvedValue(undefined);
    render(
      <TeamBillingCard workspacePriceUsd={50} ownerRole="admin" isSubscribed onManage={onManage} />,
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
      <TeamBillingCard workspacePriceUsd={50} ownerRole="admin" onSubscribe={vi.fn()} />,
    );
    expect(container.querySelector('[class*="settings-"]')).toBeNull();

    const card = screen.getByTestId("team-billing-card");
    expect(card.className).toMatch(/bg-cream(?![-\w])/);
    expect(card.className).toMatch(/\bborder\b/);
    expect(card.className).toMatch(/rounded-xl/);

    const cta = screen.getByRole("button", { name: /subscribe workspace/i });
    expect(cta.className).toMatch(/bg-\[var\(--midnight\)\]/);
    expect(cta.className).toMatch(/rounded-full/);
  });

  // `action.admin_only` is a wire field on the gate payload, not an assumption
  // the card gets to make.
  it("honors adminOnly=false by showing the CTA to a member", () => {
    const onSubscribe = vi.fn().mockResolvedValue(undefined);
    render(
      <TeamBillingCard
        workspacePriceUsd={50}
        ownerRole="member"
        adminOnly={false}
        onSubscribe={onSubscribe}
      />,
    );
    expect(screen.getByRole("button", { name: /subscribe workspace/i })).toBeInTheDocument();
    expect(screen.queryByText(/ask your org admin/i)).toBeNull();
  });

  it("keeps the CTA hidden for a member when adminOnly is explicitly true", () => {
    render(<TeamBillingCard workspacePriceUsd={50} ownerRole="member" adminOnly />);
    expect(screen.getByText(/ask your workspace admin/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /subscribe/i })).toBeNull();
  });

});
