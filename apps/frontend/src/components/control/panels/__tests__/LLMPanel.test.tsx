import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { LLMPanel } from "../LLMPanel";

const mockSWRData = vi.fn();
const mockMutate = vi.fn();
vi.mock("swr", () => ({
  default: () => ({ data: mockSWRData(), error: null, isLoading: false, mutate: mockMutate }),
}));

const mockApiPut = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({
    get: vi.fn(),
    post: vi.fn(),
    put: mockApiPut,
  }),
}));

const mockDisconnect = vi.fn();
vi.mock("@/hooks/useChatGPTOAuth", () => ({
  useChatGPTOAuth: () => ({
    disconnect: mockDisconnect,
    state: { status: "idle" },
    start: vi.fn(),
  }),
}));

// next/navigation isn't available in jsdom by default. useSearchParams
// powers the ``?action=change-provider`` auto-open on mount; stub it to
// return an empty params map so the modal stays closed unless the test
// explicitly drives it open.
const mockSearchParams = new URLSearchParams("");
vi.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

// ChangeProviderModal renders a Radix Dialog portal — stub it out so the
// existing LLMPanel tests don't have to teach the testing-library about
// the Radix portal mount root. We just check that opening the modal is
// triggered correctly via the dialog id; the modal's own behaviour is
// covered in ChangeProviderModal tests.
vi.mock("../ChangeProviderModal", () => ({
  ChangeProviderModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="change-provider-modal-open" /> : null,
}));

// Clerk's useOrganization is pulled in transitively by ChangeProviderModal
// (used to filter the chatgpt_oauth card on org context). The mock above
// makes that path inert for these tests, but the import-time path still
// resolves through ChangeProviderModal.tsx → @clerk/nextjs. The shared
// LLMPanel render doesn't otherwise touch Clerk; stub it to a no-op org
// so any code that reads it from LLMPanel directly stays happy.
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: null, membership: null }),
}));

describe("LLMPanel", () => {
  beforeEach(() => {
    mockSWRData.mockReset();
    mockMutate.mockReset();
    mockApiPut.mockReset();
    mockDisconnect.mockReset();
  });

  it("renders the ChatGPT hero + Connected status + Disconnect button for chatgpt_oauth", () => {
    mockSWRData.mockReturnValue({ provider_choice: "chatgpt_oauth" });
    render(<LLMPanel />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /disconnect/i })).toBeInTheDocument();
  });

  it("calls disconnect, then revalidates SWR after it resolves", async () => {
    mockSWRData.mockReturnValue({ provider_choice: "chatgpt_oauth" });
    mockDisconnect.mockResolvedValueOnce(undefined);
    render(<LLMPanel />);
    fireEvent.click(screen.getByRole("button", { name: /disconnect/i }));
    await waitFor(() => expect(mockDisconnect).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mockMutate).toHaveBeenCalled());
    expect(mockDisconnect.mock.invocationCallOrder[0]).toBeLessThan(
      mockMutate.mock.invocationCallOrder[0],
    );
  });

  it("clears busy state and does not revalidate when disconnect fails", async () => {
    mockSWRData.mockReturnValue({ provider_choice: "chatgpt_oauth" });
    mockDisconnect.mockRejectedValueOnce(new Error("network"));
    // The component re-throws the rejection out of the click handler; swallow
    // it on both window (jsdom) and process (node) so vitest's unhandled-
    // rejection guard doesn't fail the run.
    const swallowWin = (e: PromiseRejectionEvent) => e.preventDefault();
    const swallowProc = () => {};
    window.addEventListener("unhandledrejection", swallowWin);
    process.on("unhandledRejection", swallowProc);
    try {
      render(<LLMPanel />);
      const button = screen.getByRole("button", { name: /disconnect/i });
      fireEvent.click(button);
      await waitFor(() => expect(mockDisconnect).toHaveBeenCalled());
      // Busy state cleared (label flips back from "Disconnecting…").
      await waitFor(() => expect(button).toHaveTextContent(/^Disconnect$/));
      // mutate should not have been called on the error path.
      expect(mockMutate).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("unhandledrejection", swallowWin);
      process.off("unhandledRejection", swallowProc);
    }
  });

  it("renders the OpenAI hero + Replace key form for byo_key + openai", () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: "openai" });
    render(<LLMPanel />);
    expect(screen.getByText("Bring your own OpenAI key")).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/sk-proj-/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save/i })).toBeInTheDocument();
  });

  it("renders the Anthropic hero for byo_key + anthropic", () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: "anthropic" });
    render(<LLMPanel />);
    expect(screen.getByText("Bring your own Anthropic key")).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/sk-ant-/)).toBeInTheDocument();
  });

  it("calls onPanelChange('credits') from the Bedrock Manage-credits button when prop provided", () => {
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    const onPanelChange = vi.fn();
    render(<LLMPanel onPanelChange={onPanelChange} />);
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /manage credits/i }));
    expect(onPanelChange).toHaveBeenCalledWith("credits");
  });

  it("falls back to window.location when onPanelChange is omitted on Bedrock", () => {
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    const originalLocation = window.location;
    // jsdom: replace window.location with a writable stub
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...originalLocation, href: "" },
    });
    render(<LLMPanel />);
    fireEvent.click(screen.getByRole("button", { name: /manage credits/i }));
    expect(window.location.href).toBe("/chat?panel=credits");
    Object.defineProperty(window, "location", {
      configurable: true,
      value: originalLocation,
    });
  });

  it("renders the empty-state with an in-place Pick-a-provider button when provider_choice is null", () => {
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    // The empty-state copy uses a typographic apostrophe; match either.
    expect(screen.getByText(/haven.{1,3}t picked a provider/i)).toBeInTheDocument();
    // Now offers in-place picker instead of bouncing to /onboarding —
    // /onboarding's ProviderPicker requires no subscription, but this
    // empty-state is reachable by *active subscribers* who disconnected
    // ChatGPT OAuth (which clears provider_choice). Sending them to
    // /onboarding would 409 already_subscribed at trial-checkout.
    const button = screen.getByRole("button", { name: /pick a provider/i });
    expect(button).toBeInTheDocument();
    fireEvent.click(button);
    expect(screen.getByTestId("change-provider-modal-open")).toBeInTheDocument();
  });

  it("renders the BYOK-incomplete empty-state with an in-place Pick-a-provider button when byo_provider is missing", () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: null });
    render(<LLMPanel />);
    expect(
      screen.getByText(/bring-your-own-key configuration is incomplete/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /pick a provider/i })).toBeInTheDocument();
    // Should NOT render the BYO hero/form for an incomplete config.
    expect(screen.queryByText(/Bring your own (OpenAI|Anthropic) key/)).not.toBeInTheDocument();
  });

  it("renders a Change provider button on every populated block and opens the modal", () => {
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    render(<LLMPanel />);
    expect(screen.queryByTestId("change-provider-modal-open")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /change provider/i }));
    expect(screen.getByTestId("change-provider-modal-open")).toBeInTheDocument();
  });

  it("surfaces a save error from PUT /settings/keys/{provider}", async () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: "openai" });
    mockApiPut.mockRejectedValueOnce(new Error("Invalid key"));
    render(<LLMPanel />);
    const input = screen.getByPlaceholderText(/sk-proj-/);
    fireEvent.change(input, { target: { value: "sk-proj-bad" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(screen.getByText("Invalid key")).toBeInTheDocument());
  });
});
