import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";

import { ProviderPicker } from "../ProvisioningStepper";

const billing = vi.hoisted(() => ({
  query: "",
  role: "org:admin",
  api: { get: vi.fn().mockResolvedValue({ connected: false }), post: vi.fn() },
  startCheckout: vi.fn().mockResolvedValue(undefined),
  changeProvider: vi.fn().mockResolvedValue({ status: "ok" }),
  account: { workspace_price_usd: 75, ultra_price_usd: 150, is_subscribed: false, managed_inference_available: false, provider_choice: null as string | null },
}));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => billing }));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: { role: billing.role } }) }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(billing.query) }));
vi.mock("@/lib/api", () => ({ useApi: () => billing.api }));
vi.mock("@/lib/shellNav", () => ({ replaceSearchParams: (params: URLSearchParams) => { billing.query = params.toString(); } }));

vi.mock("../ChatGPTOAuthStep", () => ({ ChatGPTOAuthStep: ({ onComplete }: { onComplete: () => Promise<void> }) =>
  <button onClick={() => void onComplete()}>Finish test sign-in</button> }));

describe("ProviderPicker", () => {
  beforeEach(() => {
    billing.query = "";
    billing.role = "org:admin";
    billing.api.get.mockResolvedValue({ connected: false });
    billing.changeProvider.mockReset().mockResolvedValue({ status: "ok" });
    billing.startCheckout.mockClear();
    billing.account.is_subscribed = false;
    billing.account.managed_inference_available = false;
    billing.account.provider_choice = null;
  });
  it("shows the two selectable inference paths before checkout", () => {
    render(<ProviderPicker isOrg={true} orgName="Acme" />);

    expect(screen.getAllByText("$75 / month")).toHaveLength(2);
    expect(screen.getByText(/Select a provider, then continue to workspace checkout/)).toBeInTheDocument();
    expect(screen.getByText(/separate ChatGPT subscription/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "OpenRouter managed" })).toBeInTheDocument();
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.queryByText(/bring your own api key/i)).not.toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(2);
    expect(screen.queryByText(/Claude credits — no second step/)).not.toBeInTheDocument();
  });

  it("returns from checkout to the selected managed setup path", () => {
    render(<ProviderPicker isOrg={true} />);
    expect(screen.getAllByRole("button", { name: /subscribe to workspace plan/i })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /subscribe to workspace plan/i }));

    expect(billing.startCheckout).toHaveBeenCalledWith("workspace", false, "managed_onboarding");
  });

  it("returns from checkout to ChatGPT sign-in when ChatGPT is selected", () => {
    render(<ProviderPicker isOrg={true} />);
    fireEvent.click(screen.getByRole("button", { name: /sign in with chatgpt/i }));
    fireEvent.click(screen.getByRole("button", { name: /subscribe to workspace plan/i }));

    expect(billing.startCheckout).toHaveBeenCalledWith("workspace", false, "chatgpt_onboarding");
  });

  it("shows managed workspace credits to a subscribed owner when available", () => {
    billing.account.is_subscribed = true;
    billing.account.managed_inference_available = true;
    billing.account.provider_choice = "openrouter_managed";
    render(<ProviderPicker isOrg={true} />);
    expect(screen.getByRole("heading", { name: "OpenRouter managed" })).toBeInTheDocument();
    expect(screen.queryByText("Powered by Claude")).toBeNull();
    expect(screen.getByText(/Current provider:/)).toHaveTextContent("OpenRouter managed");
  });

  it("shows the saved legacy provider and changes only after explicit confirmation", async () => {
    billing.account.is_subscribed = true;
    billing.account.provider_choice = "byo_key";
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<ProviderPicker isOrg />);
    expect(screen.getByText(/Current provider:/)).toHaveTextContent("Existing saved API key (legacy)");
    fireEvent.click(screen.getByRole("button", { name: /^OpenRouter managed/i }));
    fireEvent.click(screen.getByRole("button", { name: /Use OpenRouter managed/i }));
    await waitFor(() => expect(billing.changeProvider).toHaveBeenCalledWith("openrouter_managed"));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("restarts your agents"));
    vi.restoreAllMocks();
  });

  it("keeps exactly two paths before checkout when managed credits are available after purchase", () => {
    billing.account.managed_inference_available = true;
    render(<ProviderPicker isOrg={true} />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "OpenRouter managed" })).toBeInTheDocument();
    expect(screen.queryByText(/bring your own api key/i)).not.toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(2);
  });
  it("connects ChatGPT inline and saves only after successful authentication", async () => {
    billing.account.is_subscribed = true;
    billing.account.provider_choice = "openrouter_managed";
    render(<ProviderPicker isOrg />);
    fireEvent.click(screen.getByRole("button", { name: /sign in with chatgpt/i }));
    fireEvent.click(screen.getByRole("button", { name: /^Use ChatGPT$/i }));
    expect(billing.changeProvider).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Finish test sign-in/i }));
    expect(billing.changeProvider).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Use connected ChatGPT" }));
    await waitFor(() => expect(billing.changeProvider).toHaveBeenCalledWith("chatgpt_oauth"));
  });

  it("canceling sign-in leaves the current provider untouched", () => {
    billing.account.is_subscribed = true;
    billing.account.provider_choice = "openrouter_managed";
    render(<ProviderPicker isOrg />);
    fireEvent.click(screen.getByRole("button", { name: /sign in with chatgpt/i }));
    fireEvent.click(screen.getByRole("button", { name: /^Use ChatGPT$/i }));
    fireEvent.click(screen.getByRole("button", { name: /Cancel sign-in/i }));
    expect(billing.changeProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/Current provider:/)).toHaveTextContent("OpenRouter managed");
  });

  it("a sign-in return URL cannot switch a connected workspace without a confirmation click", async () => {
    billing.query = "action=connect-chatgpt&checkout=success";
    billing.account.is_subscribed = true;
    billing.account.provider_choice = "openrouter_managed";
    render(<ProviderPicker isOrg />);
    fireEvent.click(await screen.findByRole("button", { name: "Finish test sign-in" }));
    expect(await screen.findByRole("button", { name: "Use connected ChatGPT" })).toBeInTheDocument();
    expect(billing.changeProvider).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel sign-in" }));
    expect(billing.changeProvider).not.toHaveBeenCalled();
  });

  it("keeps the saved provider when confirming ChatGPT fails and allows canceling", async () => {
    billing.account.is_subscribed = true;
    billing.account.provider_choice = "openrouter_managed";
    billing.changeProvider.mockRejectedValueOnce(new Error("Could not save provider"));
    render(<ProviderPicker isOrg />);
    fireEvent.click(screen.getByRole("button", { name: /sign in with chatgpt/i }));
    fireEvent.click(screen.getByRole("button", { name: /^Use ChatGPT$/i }));
    fireEvent.click(screen.getByRole("button", { name: "Finish test sign-in" }));
    fireEvent.click(await screen.findByRole("button", { name: "Use connected ChatGPT" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save provider");
    expect(billing.changeProvider).toHaveBeenCalledExactlyOnceWith("chatgpt_oauth");
    fireEvent.click(screen.getByRole("button", { name: "Cancel sign-in" }));
    expect(screen.getByText(/Current provider:/)).toHaveTextContent("OpenRouter managed");
  });

  it("does not expose provider changes to members", () => {
    billing.role = "org:member";
    billing.account.is_subscribed = true;
    render(<ProviderPicker isOrg />);
    expect(screen.queryByRole("button", { name: /^Use ChatGPT$/i })).toBeNull();
    expect(screen.getByText(/Ask your workspace admin to change/)).toBeInTheDocument();
    expect(billing.changeProvider).not.toHaveBeenCalled();
  });

});
