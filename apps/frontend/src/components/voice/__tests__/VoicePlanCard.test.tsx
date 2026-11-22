// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";

const state = vi.hoisted(() => ({
  userId: "user-1",
  plan: {} as Record<string, unknown>,
  get: vi.fn(), post: vi.fn(), del: vi.fn(), open: vi.fn(),
}));
// A person's session carries no organization: the plan is the signed-in user's own.
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: state.userId, isLoaded: true }) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: state.get, post: state.post, del: state.del }) }));
vi.mock("@/lib/externalAuth", () => ({ openAuthUrl: state.open }));

import { VoicePlanCard } from "../VoicePlanCard";

function tree() {
  return <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, shouldRetryOnError: false }}><VoicePlanCard /></SWRConfig>;
}

beforeEach(() => {
  state.userId = "user-1";
  state.plan = { ownerId: "user-1", status: "not_enrolled", available: true, planVersion: "voice-agents-v1", monthlyUsd: 250, includedMinutes: 1500, overageUsdPerMinute: 0.35 };
  state.get.mockReset().mockImplementation(async () => state.plan);
  state.post.mockReset().mockImplementation(async () => {
    state.plan = { ...state.plan, status: "pending_payment" };
    return { status: "pending_payment" };
  });
  state.del.mockReset().mockImplementation(async () => {
    state.plan = { ...state.plan, status: "canceling", endsAt: "2026-12-01T00:00:00Z" };
    return { status: "canceling" };
  });
  state.open.mockReset().mockResolvedValue(true);
});

it("test_scopes_keyed_on_user: requires explicit terms acceptance and binds checkout to the signed-in person's account", async () => {
  render(tree());
  const button = await screen.findByRole("button", { name: "Continue to payment" });
  expect(button).toBeDisabled();
  expect(state.post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("checkbox", { name: /I agree/ }));
  fireEvent.click(button);
  await waitFor(() => expect(state.post).toHaveBeenCalledWith("/billing/voice-plan/checkout", {
    expectedOwnerId: "user-1", planVersion: "voice-agents-v1", acceptMonthlyUsd: 250, acceptIncludedMinutes: 1500, acceptOverageUsdPerMinute: 0.35,
  }));
  expect(await screen.findByText("Waiting for payment to confirm")).toBeInTheDocument();
  expect(screen.queryByText("Phone plan active")).not.toBeInTheDocument();
});

it("opens hosted card setup only after the accepted checkout request returns it", async () => {
  state.post.mockResolvedValue({ checkoutUrl: "https://checkout.stripe.com/c/pay_test" });
  render(tree());
  await screen.findByRole("button", { name: "Continue to payment" });
  expect(state.open).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("checkbox", { name: /I agree/ }));
  fireEvent.click(screen.getByRole("button", { name: "Continue to payment" }));
  await waitFor(() => expect(state.open).toHaveBeenCalledWith("https://checkout.stripe.com/c/pay_test"));
});

it("shows inactive enrollment without a purchase action", async () => {
  state.plan.available = false;
  render(tree());
  expect(await screen.findByText("You can’t start a plan here yet. Contact support to get started.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Continue to payment" })).not.toBeInTheDocument();
  expect(state.post).not.toHaveBeenCalled();
});

it("does not expose billing mutations for a plan that is not the signed-in person's", async () => {
  state.plan = { ...state.plan, ownerId: "org-1" };
  render(tree());
  expect(await screen.findByText("Loading your plan…")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Continue to payment" })).not.toBeInTheDocument();
  expect(screen.queryByText("Only an admin of this business can change the plan.")).not.toBeInTheDocument();
});

it("confirms cancellation inline and waits for provider-confirmed end date", async () => {
  state.plan.status = "active";
  render(tree());
  fireEvent.click(await screen.findByRole("button", { name: "Cancel renewal" }));
  expect(state.del).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm cancellation" }));
  await waitFor(() => expect(state.del).toHaveBeenCalledWith("/billing/voice-plan?expectedOwnerId=user-1"));
  expect(await screen.findByText("Renewal canceled")).toBeInTheDocument();
  expect(screen.getByText(/Access continues through/)).toHaveTextContent("UTC");
});

it("requires fresh consent when another person signs in", async () => {
  const view = render(tree());
  await screen.findByRole("button", { name: "Continue to payment" });
  fireEvent.click(screen.getByRole("checkbox", { name: /I agree/ }));
  expect(screen.getByRole("button", { name: "Continue to payment" })).toBeEnabled();
  state.userId = "user-2";
  state.plan = { ...state.plan, ownerId: "user-2" };
  view.rerender(tree());
  expect(await screen.findByRole("checkbox", { name: /I agree/ })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Continue to payment" })).toBeDisabled();
});

it("does not promise unpaid access when cancellation has a confirmed end date", async () => {
  state.plan = { ...state.plan, status: "past_due", endsAt: "2026-12-01T00:00:00Z" };
  render(tree());
  expect(await screen.findByText(/Renewal ends/)).toHaveTextContent("Access requires confirmed payment");
  expect(screen.queryByText(/Access continues through/)).not.toBeInTheDocument();
});

it("does not claim success for an unconfirmed provider write", async () => {
  state.post.mockRejectedValue(new Error("provider timeout"));
  render(tree());
  await screen.findByRole("button", { name: "Continue to payment" });
  fireEvent.click(screen.getByRole("checkbox", { name: /I agree/ }));
  fireEvent.click(screen.getByRole("button", { name: "Continue to payment" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("We couldn’t confirm that change");
  expect(screen.queryByText("Phone plan active")).not.toBeInTheDocument();
});
