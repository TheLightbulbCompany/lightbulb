import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { LLMPanel } from "../LLMPanel";

const mockSWRData = vi.fn();
const mockMutate = vi.fn();
let managedAvailable = false;
let billingLoading = false;
let accountFields: Record<string, unknown> = {};
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: { managed_inference_available: managedAvailable, ...accountFields },
    isLoading: billingLoading,
  }),
}));
vi.mock("swr", () => ({
  default: () => ({ data: mockSWRData(), error: null, isLoading: false, mutate: mockMutate }),
}));

const mockApiPut = vi.fn();
const mockApiPost = vi.fn();
vi.mock("@/lib/api", () => ({
  // ApiError is re-exported by lib/api and imported by lib/providerCommit,
  // which LLMPanel pulls in — the mock must provide it or the module graph
  // resolves it to undefined and the `instanceof` check throws.
  ApiError: class ApiError extends Error {
    body: unknown;
  },
  useApi: () => ({
    get: vi.fn(),
    post: mockApiPost,
    put: mockApiPut,
  }),
}));

// ReplaceKeyForm reports save failures as a sonner toast (toast-migrated in
// c723015d9), not inline text — assert the notify call rather than mounting a
// <Toaster />.
const mockNotifyApiError = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifyApiError: (...args: unknown[]) => mockNotifyApiError(...args),
}));

const mockDisconnect = vi.fn();
vi.mock("@/hooks/useChatGPTOAuth", () => ({
  useChatGPTOAuth: () => ({
    disconnect: mockDisconnect,
    state: { status: "idle" },
    start: vi.fn(),
  }),
}));

// models.authStatus drives the live provider-health dot. Each test seeds a
// providers[] snapshot; the mutation captures refresh:true calls. The read
// hook's mutate is a stable captured spy so we can assert the Refresh button
// revalidates the read key (the source of the dot) after busting the cache.
const mockAuthStatusData = vi.fn();
const mockRpcMutation = vi.fn();
const mockReadMutate = vi.fn();
// H4: undefined unless a test opts in — the RPC error the "grey dot" bug hid.
let mockAuthStatusError: Error | undefined;
// The read is held until the runtime is DEFINITE, so these tests declare one.
// The panel does NOT branch on it beyond that: the Hermes bridge reports the same
// provider ids llm-auth-status.ts already resolves (M6 task 10, row 22).
let statusLoaded: boolean = true;
const authStatusMethods: (string | null)[] = [];
vi.mock("@lightbulb/shared", () => ({ useStatusLoaded: () => statusLoaded }));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    authStatusMethods.push(method);
    return {
      data: method === null ? undefined : mockAuthStatusData(),
      error: method === null ? undefined : mockAuthStatusError,
      isLoading: false,
      mutate: mockReadMutate,
    };
  },
  useRuntimeRpcMutation: () => mockRpcMutation,
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
  ChangeProviderModal: ({
    open,
    initialChoice,
  }: {
    open: boolean;
    initialChoice?: string | null;
  }) =>
    open ? (
      <div
        data-testid="change-provider-modal-open"
        data-initial-choice={initialChoice ?? ""}
      />
    ) : null,
}));

// CreditsSection owns its own data fetching (billing/credits SWR) and is
// covered by its own test file — stub it so LLMPanel tests only assert
// when the panel chooses to mount it (openrouter_managed owners only).
vi.mock("../CreditsSection", () => ({
  CreditsSection: () => <div data-testid="credits-section" />,
}));


describe("LLMPanel", () => {
  beforeEach(() => {
    managedAvailable = false;
    billingLoading = false;
    accountFields = { subscription_status: "active", plan: "standard" };
    mockSWRData.mockReset();
    mockMutate.mockReset();
    mockApiPut.mockReset();
    mockApiPost.mockReset();
    mockApiPost.mockResolvedValue({});
    mockNotifyApiError.mockReset();
    mockDisconnect.mockReset();
    mockAuthStatusData.mockReset();
    mockAuthStatusData.mockReturnValue({ ts: 1, providers: [] });
    mockAuthStatusError = undefined;
    mockRpcMutation.mockReset();
    // The refresh handler chains ``.then(revalidate)`` off the mutation, so
    // the mutation mock must resolve or the chain throws on undefined.
    mockRpcMutation.mockResolvedValue({ ts: 2, providers: [] });
    mockReadMutate.mockReset();
  });

  it("renders the ChatGPT hero + Connected status + Disconnect button for chatgpt_oauth", () => {
    mockSWRData.mockReturnValue({ provider_choice: "chatgpt_oauth" });
    render(<LLMPanel />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /disconnect/i })).toBeInTheDocument();
  });

  it("shows the provider's usage limit until it resets, then Connected again", () => {
    mockSWRData.mockReturnValue({ provider_choice: "chatgpt_oauth" });
    accountFields = { provider_quota_until: Math.floor(Date.now() / 1000) + 3600 };
    const { unmount } = render(<LLMPanel />);
    expect(screen.getByText(/^Usage limit reached — resets /)).toBeInTheDocument();
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
    unmount();
    accountFields = { provider_quota_until: Math.floor(Date.now() / 1000) - 60 };
    render(<LLMPanel />);
    expect(screen.getByText("Connected")).toBeInTheDocument();
    accountFields = {};
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

  it("renders the Model & Credits heading", () => {
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    render(<LLMPanel />);
    expect(screen.getByText("Model & Credits")).toBeInTheDocument();
    expect(screen.queryByText("LLM Provider")).toBeNull();
  });

  it("embeds CreditsSection for managed users and drops the Manage credits button", () => {
    managedAvailable = true;
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    render(<LLMPanel />);
    expect(screen.getByText("Lightbulb-managed models")).toBeInTheDocument();
    expect(screen.getByTestId("credits-section")).toBeInTheDocument();
    // The credits surface now lives inline — the old jump-to-panel button
    // must be gone so there's no dead affordance pointing at the retired
    // standalone Credits panel.
    expect(screen.queryByRole("button", { name: /manage credits/i })).toBeNull();
  });

  it("shows Lightbulb-managed inference and its credits for a migrated owner", () => {
    managedAvailable = true;
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    render(<LLMPanel />);
    expect(screen.getByText("Lightbulb-managed models")).toBeInTheDocument();
    expect(screen.getByText("Inference uses your Lightbulb workspace credits")).toBeInTheDocument();
    expect(screen.getByTestId("credits-section")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/sk-/)).toBeNull();
  });

  it("hides CreditsSection for non-managed users", () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: "anthropic" });
    render(<LLMPanel />);
    expect(screen.queryByTestId("credits-section")).toBeNull();
  });

  it("warns an explicit managed owner when prepaid credits can't be bought, and hides credits", () => {
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    render(<LLMPanel />);
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Prepaid credits aren.t available yet/)).toBeInTheDocument();
    expect(screen.queryByTestId("credits-section")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /change provider/i }));
    expect(screen.getByTestId("change-provider-modal-open")).toBeInTheDocument();
  });

  it("shows the daily-allowance managed path as active when provider_choice is null", () => {
    managedAvailable = true;
    accountFields = { subscription_status: "active", plan: "base" };
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    expect(screen.queryByText(/haven.{1,3}t picked a provider/i)).toBeNull();
    expect(screen.getByText("Lightbulb-managed models")).toBeInTheDocument();
    expect(screen.getByText("DeepSeek within a daily allowance")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    // The managed path is not a pickable card: choosing it would move the owner onto
    // prepaid credits (the backend refuses that until they can be bought).
    expect(screen.queryByRole("button", { name: /Lightbulb-managed/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Sign in with ChatGPT/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Bring your own API key/ })).toBeEnabled();
    expect(screen.queryByText(/coming soon/i)).toBeNull();
    expect(screen.queryByTestId("change-provider-modal-open")).not.toBeInTheDocument();
    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it("shows the used daily allowance with an Upgrade link in place of Active", () => {
    accountFields = { subscription_status: "active", plan: "base", capped_today: true };
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    expect(screen.queryByText("Active")).toBeNull();
    expect(screen.getByText("Daily allowance used")).toBeInTheDocument();
    expect(screen.getByText(/resets at 00:00 UTC/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upgrade" })).toHaveAttribute("href", "/settings?panel=billing");
  });

  it("shows how much of today's allowance is used, until the cap trips", () => {
    const allowance = { used_usd: 0.155, limit_usd: 0.25, resets_at: 0 };
    accountFields = { subscription_status: "trial_local", allowance };
    mockSWRData.mockReturnValue({ provider_choice: null });
    const { unmount } = render(<LLMPanel />);
    expect(screen.getByText("Today: 62% of your daily allowance used · resets 00:00 UTC")).toBeInTheDocument();
    unmount();

    accountFields = { subscription_status: "trial_local", allowance, capped_today: true };
    render(<LLMPanel />);
    expect(screen.getByText("Daily allowance used")).toBeInTheDocument();
    expect(screen.queryByText(/Today:/)).toBeNull();
  });

  it("tells a Workspace owner prepaid credits are coming soon", () => {
    accountFields = { subscription_status: "active", plan: "standard" };
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    expect(screen.getByText(/prepaid credits are coming soon/i)).toBeInTheDocument();
  });

  it("points a trial owner at a paid plan instead of a switch that would 409", () => {
    accountFields = { subscription_status: "trial_local", plan: "standard" };
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    expect(screen.getByRole("link", { name: "See plans" })).toHaveAttribute("href", "/settings?panel=billing");
    // Locked cards are focusable links to Billing that say why, not silent disabled buttons.
    for (const name of [/Sign in with ChatGPT/, /Bring your own API key/]) {
      const card = screen.getByRole("link", { name });
      expect(card).toHaveAttribute("href", "/settings?panel=billing");
      expect(card).toHaveTextContent("Paid plans");
      expect(card).toHaveAccessibleDescription(/Available on paid plans/);
    }
    expect(screen.queryByRole("button", { name: /Sign in with ChatGPT/ })).toBeNull();
    expect(screen.queryByText(/coming soon/i)).toBeNull();
  });

  it.each([
    [{ subscription_status: "trial_local", trial_end: Math.floor(Date.now() / 1000) - 60 }, /reading-only/i],
    [{ subscription_status: "canceled", plan: "standard" }, /No active subscription/],
    [{ subscription_status: "past_due", plan: "base" }, /No active subscription/],
    [{}, /No active subscription/],
  ])("shows managed as paused, not active, when the backend refuses it (%o)", (fields, reason) => {
    accountFields = fields;
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.queryByText("Active")).toBeNull();
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See plans" })).toHaveAttribute("href", "/settings?panel=billing");
    expect(screen.getByRole("link", { name: /Sign in with ChatGPT/ })).toHaveAttribute("href", "/settings?panel=billing");
  });

  it("treats a trial with no trial_end yet as live", () => {
    accountFields = { subscription_status: "trial_local", trial_end: null };
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("waits for billing before rendering a first provider choice", () => {
    billingLoading = true;
    mockSWRData.mockReturnValue({ provider_choice: null });
    const view = render(<LLMPanel />);
    expect(screen.queryByRole("button", { name: /Sign in with ChatGPT/ })).toBeNull();
    billingLoading = false;
    view.rerender(<LLMPanel />);
    expect(screen.getByRole("button", { name: /Sign in with ChatGPT/ })).toBeInTheDocument();
  });

  it.each([
    [/Sign in with ChatGPT/, "chatgpt_oauth"],
    [/Bring your own API key/, "byo_key"],
  ])("opens the modal on the %s credential stage", async (label, choice) => {
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    fireEvent.click(screen.getByRole("button", { name: label }));
    const modal = screen.getByTestId("change-provider-modal-open");
    // initialChoice skips the modal's own picker — the user already picked.
    expect(modal).toHaveAttribute("data-initial-choice", choice);
    // Credential paths must NOT commit from the panel.
    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it("renders the inline choices for the BYOK-incomplete empty-state", () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: null });
    render(<LLMPanel />);
    expect(
      screen.getByText(/bring-your-own-key configuration is incomplete/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sign in with ChatGPT/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Bring your own API key/ })).toBeInTheDocument();
    // Should NOT render the BYO hero/form for an incomplete config.
    expect(screen.queryByText(/Bring your own (OpenAI|Anthropic) key/)).not.toBeInTheDocument();
  });

  it("renders a Change provider button on every populated block and opens the modal", () => {
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    render(<LLMPanel />);
    expect(screen.queryByTestId("change-provider-modal-open")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /change provider/i }));
    const modal = screen.getByTestId("change-provider-modal-open");
    expect(modal).toBeInTheDocument();
    // A deliberate switch opens on the full picker (with its cutover warning),
    // not pre-advanced to a credential stage.
    expect(modal).toHaveAttribute("data-initial-choice", "");
  });

  it("surfaces a save error from PUT /settings/keys/{provider}", async () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: "openai" });
    const err = new Error("Invalid key");
    mockApiPut.mockRejectedValueOnce(err);
    render(<LLMPanel />);
    const input = screen.getByPlaceholderText(/sk-proj-/);
    fireEvent.change(input, { target: { value: "sk-proj-bad" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(mockNotifyApiError).toHaveBeenCalledWith(err, "Couldn't save key"),
    );
  });

  it("colors the provider dot green when models.authStatus reports ok", () => {
    managedAvailable = true;
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "custom", displayName: "Lightbulb", status: "ok" }],
    });
    render(<LLMPanel />);
    const dot = screen.getByTestId("provider-health-dot");
    expect(dot).toHaveAttribute("data-color", "green");
  });

  it("colors the provider dot red when the provider auth is expired", () => {
    mockSWRData.mockReturnValue({ provider_choice: "chatgpt_oauth" });
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "openai-codex", displayName: "ChatGPT", status: "expired" }],
    });
    render(<LLMPanel />);
    expect(screen.getByTestId("provider-health-dot")).toHaveAttribute("data-color", "red");
  });

  it("colors the provider dot amber when the provider auth is expiring", () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: "anthropic" });
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "anthropic", displayName: "Anthropic", status: "expiring" }],
    });
    render(<LLMPanel />);
    expect(screen.getByTestId("provider-health-dot")).toHaveAttribute("data-color", "amber");
  });

  it("falls back to a neutral dot when no provider entry matches", () => {
    managedAvailable = true;
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    mockAuthStatusData.mockReturnValue({ ts: 1, providers: [] });
    render(<LLMPanel />);
    expect(screen.getByTestId("provider-health-dot")).toHaveAttribute("data-color", "neutral");
  });

  it("holds models.authStatus while the runtime is unknown, and a neutral dot with it", () => {
    statusLoaded = false;
    authStatusMethods.length = 0;
    managedAvailable = true;
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "custom", displayName: "Lightbulb", status: "ok" }],
    });
    render(<LLMPanel />);
    // Only this panel's own read is asserted — a child card fires `config.get`,
    // which belongs to a different lane.
    expect(authStatusMethods).not.toContain("models.authStatus");
    expect(screen.getByTestId("provider-health-dot")).toHaveAttribute("data-color", "neutral");
  });

  it("resolves a BYO-key dot from the Hermes provider id (openai-api)", () => {
    statusLoaded = true;
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: "openai" });
    // What the bridge reports for a BYO-OpenAI profile — `llm-auth-status.ts` matches it
    // through its `openai-` prefix rule with no AgentCore branch of its own.
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "openai-api", displayName: "OpenAI", status: "ok" }],
    });
    render(<LLMPanel />);
    expect(screen.getByTestId("provider-health-dot")).toHaveAttribute("data-color", "green");
  });

  it("opens the change-provider modal from the initialAction prop", () => {
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    render(<LLMPanel initialAction="change-provider" />);
    expect(screen.getByTestId("change-provider-modal-open")).toBeInTheDocument();
  });

  it("keeps the provider_required change-provider handoff available for an active workspace with no provider", () => {
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel initialAction="change-provider" />);
    expect(screen.getByTestId("change-provider-modal-open")).toBeInTheDocument();
    // The null-provider picker commit path is covered by the no-POST
    // assertions elsewhere in this file; this handoff must never redirect to Checkout.
    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it("re-opens the modal when initialAction goes null→change-provider on a mounted panel", () => {
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    const { rerender } = render(<LLMPanel initialAction={null} />);
    expect(
      screen.queryByTestId("change-provider-modal-open"),
    ).not.toBeInTheDocument();
    // A second command to an already-mounted panel must re-pop the modal —
    // the old useState-initializer-only read ignored it.
    rerender(<LLMPanel initialAction="change-provider" />);
    expect(screen.getByTestId("change-provider-modal-open")).toBeInTheDocument();
  });

  it("reports consumption via onActionConsumed when initialAction is delivered", async () => {
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    const onActionConsumed = vi.fn();
    render(
      <LLMPanel initialAction="change-provider" onActionConsumed={onActionConsumed} />,
    );
    await waitFor(() => expect(onActionConsumed).toHaveBeenCalledTimes(1));
  });

  it("busts the gateway cache then revalidates the read key when Refresh is clicked", async () => {
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "custom", displayName: "Lightbulb", status: "ok" }],
    });
    render(<LLMPanel />);
    fireEvent.click(screen.getByRole("button", { name: /refresh status/i }));
    // Busts the gateway's 60s cache snapshot...
    expect(mockRpcMutation).toHaveBeenCalledWith("models.authStatus", { refresh: true });
    // ...then revalidates the read hook so the SWR cache (and the dot) updates.
    // The read hook only auto-revalidates on a matching gateway *event*, and
    // models.authStatus is an RPC with no event, so without this explicit
    // revalidate the dot keeps its stale color until an unrelated remount.
    await waitFor(() => expect(mockReadMutate).toHaveBeenCalled());
    expect(mockRpcMutation.mock.invocationCallOrder[0]).toBeLessThan(
      mockReadMutate.mock.invocationCallOrder[0],
    );
  });

  // H4: a models.authStatus RPC failure used to be silent — resolveProviderStatus
  // fell through to undefined, rendering the same neutral dot as "still loading".
  it("surfaces a models.authStatus RPC failure with a retry, instead of failing silently", () => {
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    mockAuthStatusError = new Error("gateway unreachable");
    render(<LLMPanel />);
    expect(screen.getByText("Couldn't check provider status.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(mockReadMutate).toHaveBeenCalled();
  });

  it("shows no error state when models.authStatus has not failed", () => {
    mockSWRData.mockReturnValue({ provider_choice: "openrouter_managed" });
    render(<LLMPanel />);
    expect(screen.queryByText("Couldn't check provider status.")).toBeNull();
  });
});
