import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import React from "react";
import { LLMPanel } from "../LLMPanel";

const mockSWRData = vi.fn();
const mockMutate = vi.fn();
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
// The harness card calls the same hook with basePath "/oauth/chatgpt/harness"
// (second OAuth grant). Separate spies + a seedable state so tests can drive
// the harness card through idle/pending/completed without touching the
// provider block's mock.
const mockHarnessDisconnect = vi.fn();
const mockHarnessStart = vi.fn();
const mockHarnessState = vi.fn(() => ({ status: "idle" }) as unknown);
vi.mock("@/hooks/useChatGPTOAuth", () => ({
  useChatGPTOAuth: (opts?: { basePath?: string }) =>
    opts?.basePath === "/oauth/chatgpt/harness"
      ? {
          disconnect: mockHarnessDisconnect,
          state: mockHarnessState(),
          start: mockHarnessStart,
        }
      : {
          disconnect: mockDisconnect,
          state: { status: "idle" },
          start: vi.fn(),
        },
}));

// models.authStatus drives the live provider-health dot. Each test seeds a
// providers[] snapshot; the mutation captures refresh:true calls. The read
// hook's mutate is a stable captured spy so we can assert the Refresh button
// revalidates the read key (the source of the dot) after busting the cache.
const mockAuthStatusData = vi.fn();
const mockRpcMutation = vi.fn();
const mockReadMutate = vi.fn();
// The read is held until the runtime is DEFINITE, so these tests declare one.
// The panel does NOT branch on it beyond that: the Hermes bridge reports the same
// provider ids llm-auth-status.ts already resolves (M6 task 10, row 22).
let runtimeHost: "ecs" | "agentcore" | undefined = "ecs";
const authStatusMethods: (string | null)[] = [];
vi.mock("@lightbulb/shared", () => ({ useRuntimeHost: () => runtimeHost }));
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    authStatusMethods.push(method);
    return {
      data: method === null ? undefined : mockAuthStatusData(),
      error: undefined,
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
// when the panel chooses to mount it (Bedrock owners only).
vi.mock("../CreditsSection", () => ({
  CreditsSection: () => <div data-testid="credits-section" />,
}));


describe("LLMPanel", () => {
  beforeEach(() => {
    mockSWRData.mockReset();
    mockMutate.mockReset();
    mockApiPut.mockReset();
    mockApiPost.mockReset();
    mockApiPost.mockResolvedValue({});
    mockNotifyApiError.mockReset();
    mockDisconnect.mockReset();
    mockAuthStatusData.mockReset();
    mockAuthStatusData.mockReturnValue({ ts: 1, providers: [] });
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
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    render(<LLMPanel />);
    expect(screen.getByText("Model & Credits")).toBeInTheDocument();
    expect(screen.queryByText("LLM Provider")).toBeNull();
  });

  it("embeds CreditsSection for bedrock users and drops the Manage credits button", () => {
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    render(<LLMPanel />);
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
    expect(screen.getByTestId("credits-section")).toBeInTheDocument();
    // The credits surface now lives inline — the old jump-to-panel button
    // must be gone so there's no dead affordance pointing at the retired
    // standalone Credits panel.
    expect(screen.queryByRole("button", { name: /manage credits/i })).toBeNull();
  });

  it("hides CreditsSection for non-bedrock users", () => {
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: "anthropic" });
    render(<LLMPanel />);
    expect(screen.queryByTestId("credits-section")).toBeNull();
  });

  it("renders all three provider choices inline when provider_choice is null", () => {
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    // The empty-state copy uses a typographic apostrophe; match either.
    expect(screen.getByText(/haven.{1,3}t picked a provider/i)).toBeInTheDocument();
    // The choices are visible without a click — the old single "Pick a
    // provider" button hid them behind a modal, so a user with no provider
    // couldn't see what the options even were.
    expect(screen.getByRole("button", { name: /Powered by Claude/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sign in with ChatGPT/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Bring your own API key/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^pick a provider/i })).toBeNull();
    // Nothing opens until a choice is made.
    expect(screen.queryByTestId("change-provider-modal-open")).not.toBeInTheDocument();
  });

  it("commits Bedrock in place without opening the modal", async () => {
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel />);
    fireEvent.click(screen.getByRole("button", { name: /Powered by Claude/ }));
    await waitFor(() =>
      expect(mockApiPost).toHaveBeenCalledWith("/billing/provider", {
        provider_choice: "bedrock_claude",
      }),
    );
    // Bedrock needs no credential collection, so the modal must stay shut.
    expect(screen.queryByTestId("change-provider-modal-open")).not.toBeInTheDocument();
    await waitFor(() => expect(mockMutate).toHaveBeenCalled());
  });

  it("surfaces a commit error inline when the Bedrock pick fails", async () => {
    mockSWRData.mockReturnValue({ provider_choice: null });
    mockApiPost.mockRejectedValueOnce(new Error("No credits"));
    render(<LLMPanel />);
    fireEvent.click(screen.getByRole("button", { name: /Powered by Claude/ }));
    await waitFor(() => expect(screen.getByText("No credits")).toBeInTheDocument());
    expect(mockMutate).not.toHaveBeenCalled();
    // Cards must be re-enabled so the user can pick a different path.
    expect(
      screen.getByRole("button", { name: /Sign in with ChatGPT/ }),
    ).not.toBeDisabled();
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
    expect(screen.getByRole("button", { name: /Powered by Claude/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Bring your own API key/ })).toBeInTheDocument();
    // Should NOT render the BYO hero/form for an incomplete config.
    expect(screen.queryByText(/Bring your own (OpenAI|Anthropic) key/)).not.toBeInTheDocument();
  });

  it("renders a Change provider button on every populated block and opens the modal", () => {
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
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
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "bedrock", displayName: "Bedrock", status: "ok" }],
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
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    mockAuthStatusData.mockReturnValue({ ts: 1, providers: [] });
    render(<LLMPanel />);
    expect(screen.getByTestId("provider-health-dot")).toHaveAttribute("data-color", "neutral");
  });

  it("holds models.authStatus while the runtime is unknown, and a neutral dot with it", () => {
    runtimeHost = undefined;
    authStatusMethods.length = 0;
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "bedrock", displayName: "Bedrock", status: "ok" }],
    });
    render(<LLMPanel />);
    // Only this panel's own read is asserted — a child card fires `config.get`,
    // which belongs to a different lane.
    expect(authStatusMethods).not.toContain("models.authStatus");
    expect(screen.getByTestId("provider-health-dot")).toHaveAttribute("data-color", "neutral");
    runtimeHost = "ecs";
  });

  it("resolves a BYO-key dot from the Hermes provider id (openai-api)", () => {
    runtimeHost = "agentcore";
    mockSWRData.mockReturnValue({ provider_choice: "byo_key", byo_provider: "openai" });
    // What the bridge reports for a BYO-OpenAI profile — `llm-auth-status.ts` matches it
    // through its `openai-` prefix rule with no AgentCore branch of its own.
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "openai-api", displayName: "OpenAI", status: "ok" }],
    });
    render(<LLMPanel />);
    expect(screen.getByTestId("provider-health-dot")).toHaveAttribute("data-color", "green");
    runtimeHost = "ecs";
  });

  it("opens the change-provider modal from the initialAction prop", () => {
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    render(<LLMPanel initialAction="change-provider" />);
    expect(screen.getByTestId("change-provider-modal-open")).toBeInTheDocument();
  });

  it("keeps the provider_required change-provider handoff available for an active workspace with no provider", () => {
    mockSWRData.mockReturnValue({ provider_choice: null });
    render(<LLMPanel initialAction="change-provider" />);
    expect(screen.getByTestId("change-provider-modal-open")).toBeInTheDocument();
    // The null-provider picker commit path is covered immediately above by
    // the Bedrock POST assertion; this handoff must never redirect to Checkout.
    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it("re-opens the modal when initialAction goes null→change-provider on a mounted panel", () => {
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
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
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    const onActionConsumed = vi.fn();
    render(
      <LLMPanel initialAction="change-provider" onActionConsumed={onActionConsumed} />,
    );
    await waitFor(() => expect(onActionConsumed).toHaveBeenCalledTimes(1));
  });

  it("busts the gateway cache then revalidates the read key when Refresh is clicked", async () => {
    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    mockAuthStatusData.mockReturnValue({
      ts: 1,
      providers: [{ provider: "amazon-bedrock", displayName: "Amazon Bedrock", status: "ok" }],
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
});

describe("CodingHarnessBlock", () => {
  beforeEach(() => {
    mockSWRData.mockReturnValue({ provider_choice: "chatgpt_oauth" });
    mockAuthStatusData.mockReturnValue(undefined);
    mockHarnessStart.mockReset();
    mockHarnessDisconnect.mockReset();
    mockHarnessState.mockReset();
    mockHarnessState.mockReturnValue({ status: "idle" });
  });

  it("renders only for chatgpt_oauth owners", () => {
    render(<LLMPanel />);
    expect(screen.getByTestId("coding-harness-block")).toBeInTheDocument();

    mockSWRData.mockReturnValue({ provider_choice: "bedrock_claude" });
    render(<LLMPanel />);
    expect(screen.getAllByTestId("coding-harness-block")).toHaveLength(1);
  });

  it("idle: Connect button fires start, never auto-starts", () => {
    render(<LLMPanel />);
    expect(mockHarnessStart).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /connect coding harness/i }));
    expect(mockHarnessStart).toHaveBeenCalledTimes(1);
  });

  it("pending: shows the user code and verification link", () => {
    mockHarnessState.mockReturnValue({
      status: "pending",
      userCode: "HARN-1234",
      verificationUri: "https://auth.openai.com/codex/device",
      expiresAt: Date.now() + 900_000,
    });
    render(<LLMPanel />);
    expect(screen.getByText("HARN-1234")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "https://auth.openai.com/codex/device" }),
    ).toHaveAttribute("href", "https://auth.openai.com/codex/device");
  });

  it("pending + expired code: offers Get a new code instead of waiting forever", () => {
    mockHarnessState.mockReturnValue({
      status: "pending",
      userCode: "HARN-1234",
      verificationUri: "https://auth.openai.com/codex/device",
      expiresAt: Date.now() - 1000,
    });
    render(<LLMPanel />);
    expect(screen.getByText(/this code expired/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /get a new code/i }));
    expect(mockHarnessStart).toHaveBeenCalledTimes(1);
  });

  it("completed: Disconnect calls the harness disconnect, not the provider's", async () => {
    mockHarnessState.mockReturnValue({ status: "completed", accountId: "acc_h" });
    mockHarnessDisconnect.mockResolvedValueOnce(undefined);
    render(<LLMPanel />);
    const block = screen.getByTestId("coding-harness-block");
    const button = within(block).getByRole("button", { name: /disconnect/i });
    fireEvent.click(button);
    await waitFor(() => expect(mockHarnessDisconnect).toHaveBeenCalledTimes(1));
    expect(mockDisconnect).not.toHaveBeenCalled();
  });
});
