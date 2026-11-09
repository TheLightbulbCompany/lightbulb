import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ProviderPicker } from "../ProvisioningStepper";

const billing = vi.hoisted(() => ({
  account: { is_subscribed: true, provider_choice: "openrouter_managed", workspace_price_usd: 75 },
  isSubscribed: true,
  isLoading: false,
  refresh: vi.fn(),
  startCheckout: vi.fn(),
  changeProvider: vi.fn(),
}));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => billing }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: { role: "org:admin" } }) }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(window.location.search) }));

// Real OAuth child, plan gate, hook and history helper: only the API/billing/auth boundaries are mocked.
describe("ProviderPicker embedded OAuth cancellation", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.history.replaceState(null, "", "/voice/settings?provider=chatgpt_oauth&action=connect-chatgpt&checkout=success&tag=a&tag=b");
    api.get.mockReset().mockResolvedValue({ connected: false });
    api.post.mockReset().mockResolvedValue({
      user_code: "ABCD-1234", verification_uri: "https://chatgpt.com/auth/device", expires_in: 600, interval: 5,
    });
    billing.changeProvider.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    window.history.replaceState(null, "", "/");
  });

  it("requires explicit confirmation after real OAuth completes", async () => {
    render(<ProviderPicker isOrg embedded />);
    await act(async () => {});
    expect(screen.getByText("ABCD-1234")).toBeInTheDocument();
    api.post.mockResolvedValueOnce({ status: "completed", account_id: "acct_connected" });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole("button", { name: "Use connected ChatGPT" })).toBeInTheDocument();
    expect(billing.changeProvider).not.toHaveBeenCalled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Use connected ChatGPT" })); });
    expect(billing.changeProvider).toHaveBeenCalledExactlyOnceWith("chatgpt_oauth");
    expect(screen.getByText(/Current provider:/)).toHaveTextContent("Sign in with ChatGPT");
  });

  it.each([
    ["pending", "Cancel and choose a different provider"],
    ["error", "Cancel and choose a different provider"],
    ["pending", "Cancel sign-in"],
    ["error", "Cancel sign-in"],
  ])("exits %s via %s, stops polling and ignores late completion", async (state, control) => {
    let completePoll: ((value: { status: string; account_id: string }) => void) | undefined;
    const poll = new Promise((resolve) => { completePoll = resolve; });
    render(<ProviderPicker isOrg embedded />);
    await act(async () => {});
    expect(screen.getByText("ABCD-1234")).toBeInTheDocument();
    expect(billing.changeProvider).not.toHaveBeenCalled();

    if (state === "error") api.post.mockRejectedValueOnce(new Error("device code expired"));
    else api.post.mockReturnValueOnce(poll);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api.post).toHaveBeenLastCalledWith("/oauth/chatgpt/poll", {});
    if (state === "error") expect(screen.getByText("Connection failed: device code expired")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: control }));
    expect(screen.queryByText("ABCD-1234")).not.toBeInTheDocument();
    expect(screen.queryByText(/Connection failed:/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel sign-in" })).not.toBeInTheDocument();
    expect(screen.getByText(/Current provider:/)).toHaveTextContent("OpenRouter managed");
    const params = new URLSearchParams(window.location.search);
    expect(params.has("provider")).toBe(false);
    expect(params.has("action")).toBe(false);
    expect(params.get("checkout")).toBe("success");
    expect(params.getAll("tag")).toEqual(["a", "b"]);

    const callsAtCancel = api.post.mock.calls.length;
    await act(async () => {
      completePoll?.({ status: "completed", account_id: "acct_late" });
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(api.post).toHaveBeenCalledTimes(callsAtCancel);
    expect(billing.changeProvider).not.toHaveBeenCalled();
    expect(billing.startCheckout).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Use connected ChatGPT" })).not.toBeInTheDocument();
    expect(screen.getByText(/Current provider:/)).toHaveTextContent("OpenRouter managed");
  });
});
