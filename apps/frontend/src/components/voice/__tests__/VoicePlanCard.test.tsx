// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";

const state = vi.hoisted(() => ({
  admin: true,
  orgId: "org-1",
  userId: "user-1",
  plan: {} as Record<string, unknown>,
  get: vi.fn(), post: vi.fn(), del: vi.fn(), open: vi.fn(),
}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ orgId: state.orgId, userId: state.userId, isLoaded: true }) }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => state.admin }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: state.get, post: state.post, del: state.del }) }));
vi.mock("@/lib/externalAuth", () => ({ openAuthUrl: state.open }));

import { VoicePlanCard } from "../VoicePlanCard";

function tree() {
  return <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0, shouldRetryOnError: false }}><VoicePlanCard /></SWRConfig>;
}

beforeEach(() => {
  state.admin = true;
  state.orgId = "org-1";
  state.userId = "user-1";
  state.plan = { ownerId: "org-1", status: "not_enrolled", available: true, planVersion: "voice-agents-v1", monthlyUsd: 250, includedMinutes: 1500, overageUsdPerMinute: 0.35 };
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

it("requires explicit terms acceptance and binds checkout to the displayed workspace", async () => {
  render(tree());
  const button = await screen.findByRole("button", { name: "Continue to payment" });
  expect(button).toBeDisabled();
  expect(state.post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("checkbox", { name: /I agree/ }));
  fireEvent.click(button);
  await waitFor(() => expect(state.post).toHaveBeenCalledWith("/billing/voice-plan/checkout", {
    expectedOwnerId: "org-1", planVersion: "voice-agents-v1", acceptMonthlyUsd: 250, acceptIncludedMinutes: 1500, acceptOverageUsdPerMinute: 0.35,
  }));
  expect(await screen.findByText("Payment confirmation pending")).toBeInTheDocument();
  expect(screen.queryByText("Voice plan active")).not.toBeInTheDocument();
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
  expect(await screen.findByText("Voice plan enrollment isn’t available yet.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Continue to payment" })).not.toBeInTheDocument();
  expect(state.post).not.toHaveBeenCalled();
});

it("does not expose billing mutations to a member or a mismatched workspace", async () => {
  state.admin = false;
  const view = render(tree());
  expect(await screen.findByText("A workspace admin can manage the voice plan.")).toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  state.admin = true;
  state.orgId = "org-2";
  view.rerender(tree());
  expect(await screen.findByText("Loading your plan…")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Continue to payment" })).not.toBeInTheDocument();
});

it("confirms cancellation inline and waits for provider-confirmed end date", async () => {
  state.plan.status = "active";
  render(tree());
  fireEvent.click(await screen.findByRole("button", { name: "Cancel renewal" }));
  expect(state.del).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm cancellation" }));
  await waitFor(() => expect(state.del).toHaveBeenCalledWith("/billing/voice-plan?expectedOwnerId=org-1"));
  expect(await screen.findByText("Renewal canceled")).toBeInTheDocument();
  expect(screen.getByText(/Access continues through/)).toHaveTextContent("UTC");
});

it("requires fresh consent when another admin signs into the same workspace", async () => {
  const view = render(tree());
  await screen.findByRole("button", { name: "Continue to payment" });
  fireEvent.click(screen.getByRole("checkbox", { name: /I agree/ }));
  expect(screen.getByRole("button", { name: "Continue to payment" })).toBeEnabled();
  state.userId = "user-2";
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
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t confirm the update");
  expect(screen.queryByText("Voice plan active")).not.toBeInTheDocument();
});
