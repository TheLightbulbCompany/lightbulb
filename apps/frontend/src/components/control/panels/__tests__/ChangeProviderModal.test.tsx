import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { ChangeProviderModal } from "../ChangeProviderModal";

/**
 * Covers the `initialChoice` → opening-stage mapping. LLMPanel's tests mock
 * this component out entirely and can only assert the prop it passes, so a
 * swapped mapping (chatgpt_oauth landing on the BYO form, or either falling
 * through to the picker) would pass there unnoticed. These render the real
 * component and assert which stage actually comes up.
 */

const mockApiPost = vi.fn();
const mockApiPut = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    body: unknown;
  },
  useApi: () => ({ get: vi.fn(), post: mockApiPost, put: mockApiPut }),
}));

// Pending device-code state so the OAuth stage renders its distinctive copy
// (verification link + user code) rather than the transient spinner.
const mockOAuthState = vi.fn();
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));
// The plan gate in front of ChatGPT sign-in reads billing; a subscriber unless a test says otherwise.
const mockBilling = vi.hoisted(() => ({ isSubscribed: true, startCheckout: vi.fn(async () => undefined) }));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: { is_subscribed: mockBilling.isSubscribed }, isLoading: false, isSubscribed: mockBilling.isSubscribed,
    refresh: vi.fn(), startCheckout: mockBilling.startCheckout }),
}));

vi.mock("@/hooks/useChatGPTOAuth", () => ({
  useChatGPTOAuth: () => ({
    state: mockOAuthState(),
    start: vi.fn(),
    disconnect: vi.fn(),
  }),
}));

/** The picker's three cards — absent whenever a credential stage is showing. */
const pickerCards = () => screen.queryAllByRole("heading", { level: 3 });

describe("ChangeProviderModal initialChoice", () => {
  beforeEach(() => {
    mockApiPost.mockReset();
    mockApiPost.mockResolvedValue({});
    mockApiPut.mockReset();
    mockOAuthState.mockReset();
    mockOAuthState.mockReturnValue({
      status: "pending",
      verificationUri: "https://auth.openai.com/device",
      userCode: "ABCD-1234",
    });
  });

  const renderModal = (initialChoice?: "chatgpt_oauth" | "byo_key" | null, managedInferenceAvailable = false) =>
    render(
      <ChangeProviderModal
        open
        currentProvider={null}
        currentByoProvider={null}
        initialChoice={initialChoice}
        managedInferenceAvailable={managedInferenceAvailable}
        onClose={vi.fn()}
        onCommitted={vi.fn()}
      />,
    );

  it("opens on the picker when no initialChoice is given", () => {
    renderModal();
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Bring your own API key")).toBeInTheDocument();
    expect(pickerCards()).toHaveLength(2);
    expect(screen.queryByText("Powered by Claude")).toBeNull();
    expect(screen.queryByText("Lightbulb-managed models")).toBeNull();
  });

  it("offers the managed route without asking for a key when available", async () => {
    renderModal(null, true);
    expect(screen.queryByText("Powered by Claude")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Lightbulb-managed models/ }));
    await waitFor(() => expect(mockApiPost).toHaveBeenCalledWith("/billing/provider", {
      provider_choice: "openrouter_managed",
    }));
    expect(screen.queryByPlaceholderText(/sk-/)).toBeNull();
  });

  it("a free workspace picking ChatGPT sees the plan step, never a device code; Checkout comes back to sign-in", async () => {
    mockBilling.isSubscribed = false;
    try {
      renderModal("chatgpt_oauth");
      expect(screen.getByText("Signing in with ChatGPT comes with the Workspace plan.")).toBeInTheDocument();
      expect(screen.queryByText("ABCD-1234")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Choose the Workspace plan" }));
      expect(mockBilling.startCheckout).toHaveBeenCalledWith("workspace", false, "chatgpt");
    } finally { mockBilling.isSubscribed = true; }
  });

  it("opens on the ChatGPT device-code stage for chatgpt_oauth", () => {
    renderModal("chatgpt_oauth");
    expect(screen.getByText("ABCD-1234")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /auth\.openai\.com\/device/ }),
    ).toBeInTheDocument();
    // Must NOT be the BYO form, and must NOT have fallen back to the picker.
    expect(screen.queryByPlaceholderText(/sk-proj-/)).toBeNull();
    expect(pickerCards()).toHaveLength(0);
  });

  it("opens on the BYO key form for byo_key", () => {
    renderModal("byo_key");
    expect(screen.getByPlaceholderText(/sk-proj-/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /openai/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /anthropic/i })).toBeInTheDocument();
    // Must NOT be the OAuth stage, and must NOT have fallen back to the picker.
    expect(screen.queryByText("ABCD-1234")).toBeNull();
    expect(pickerCards()).toHaveLength(0);
  });

  it("renders nothing when closed", () => {
    render(
      <ChangeProviderModal
        open={false}
        initialChoice="byo_key"
        onClose={vi.fn()}
        onCommitted={vi.fn()}
      />,
    );
    expect(screen.queryByPlaceholderText(/sk-proj-/)).toBeNull();
  });
});
