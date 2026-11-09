import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { ChatGPTOAuthStep } from "../ChatGPTOAuthStep";
import { markCheckoutReturn } from "@/lib/checkoutReturn";
import type { OAuthState } from "@/hooks/useChatGPTOAuth";

const startMock = vi.fn();

interface MockHookValue {
  state: OAuthState;
  start: () => void;
  disconnect: () => Promise<void>;
}

const stateRef: { current: MockHookValue } = { current: initialState() };

function initialState(): MockHookValue {
  return { state: { status: "idle" }, start: startMock, disconnect: vi.fn() };
}

function pendingState(expiresAt: number): MockHookValue {
  return {
    state: {
      status: "pending",
      userCode: "ABCD-1234",
      verificationUri: "https://chatgpt.com/auth/device",
      expiresAt,
    },
    start: startMock,
    disconnect: vi.fn(),
  };
}

function completedState(): MockHookValue {
  return {
    state: { status: "completed", accountId: "acct_x" },
    start: startMock,
    disconnect: vi.fn(),
  };
}

// The plan gate in front of the step reads billing: a subscriber unless a test says otherwise.
const billing = vi.hoisted(() => ({ isSubscribed: true, refresh: vi.fn(), startCheckout: vi.fn(async () => undefined) }));
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: { is_subscribed: billing.isSubscribed }, isLoading: false, isSubscribed: billing.isSubscribed,
    refresh: billing.refresh, startCheckout: billing.startCheckout }),
}));

vi.mock("@/hooks/useChatGPTOAuth", () => ({
  useChatGPTOAuth: () => stateRef.current,
}));

// The step now reads useSearchParams (for the cancel/back escape). Mirror the
// CreditsStep test's next/navigation mock so useSearchParams resolves.
let searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => searchParams,
}));

// Copy-to-clipboard uses navigator.clipboard, absent in jsdom.
const writeTextMock = vi.fn().mockResolvedValue(undefined);
Object.defineProperty(navigator, "clipboard", {
  value: { writeText: writeTextMock },
  configurable: true,
  writable: true,
});

describe("ChatGPTOAuthStep", () => {
  beforeEach(() => {
    startMock.mockReset();
    writeTextMock.mockClear();
    searchParams = new URLSearchParams();
    stateRef.current = initialState();
  });

  it("renders no Connect CTA while idle — auto-start is owned by the hook", () => {
    // The on-mount status probe + auto-start moved INTO useChatGPTOAuth
    // (run-once effect), so the component never calls start() itself; the
    // hook-level tests cover the auto-start. The component's remaining
    // obligation is that the legacy idle-state CTA stays gone.
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    expect(startMock).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: /connect chatgpt/i }),
    ).not.toBeInTheDocument();
  });

  it("renders the device-code instructions while pending", () => {
    stateRef.current = pendingState(Date.now() + 600_000);
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    expect(screen.getByText("ABCD-1234")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /chatgpt.com\/auth\/device/i }),
    ).toBeInTheDocument();
  });

  it("advances once the provider is connected", async () => {
    stateRef.current = completedState();
    const onComplete = vi.fn();
    const view = render(<ChatGPTOAuthStep onComplete={onComplete} />);
    await waitFor(() => expect(onComplete).toHaveBeenCalled());
    view.rerender(<ChatGPTOAuthStep onComplete={onComplete} />);
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("does not commit a provider after OAuth fails", () => {
    stateRef.current = { ...initialState(), state: { status: "error", message: "device code expired" } };
    const onComplete = vi.fn();
    render(<ChatGPTOAuthStep onComplete={onComplete} />);
    expect(screen.getByText(/Connection failed: device code expired/)).toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("keeps a completed OAuth session available for retry when provider commit fails", async () => {
    stateRef.current = completedState();
    const onComplete = vi.fn().mockRejectedValueOnce(new Error("workspace changed"));
    render(<ChatGPTOAuthStep onComplete={onComplete} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("workspace changed");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(2));
  });

  it("copies the user code to the clipboard", async () => {
    stateRef.current = pendingState(Date.now() + 600_000);
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /copy code/i }));
    await waitFor(() => expect(writeTextMock).toHaveBeenCalledWith("ABCD-1234"));
    // Visible feedback flips to "Copied".
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("shows a live expiry countdown while the code is valid", () => {
    stateRef.current = pendingState(Date.now() + 600_000);
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    expect(screen.getByText(/code expires in/i)).toBeInTheDocument();
    expect(screen.queryByText(/this code expired/i)).not.toBeInTheDocument();
  });

  it("shows an expired affordance and re-requests a code when the device code has expired", () => {
    stateRef.current = pendingState(Date.now() - 1000);
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    expect(screen.getByText(/this code expired/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /get a new code/i }));
    expect(startMock).toHaveBeenCalled();
  });

  it("strips ?provider= when cancelling, preserving other params", () => {
    const onComplete = vi.fn();
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    searchParams = new URLSearchParams({ provider: "chatgpt_oauth", checkout: "success" });
    stateRef.current = pendingState(Date.now() + 600_000);
    render(<ChatGPTOAuthStep onComplete={onComplete} />);
    fireEvent.click(
      screen.getByRole("button", { name: /cancel and choose a different provider/i }),
    );
    const target = replaceStateSpy.mock.calls.at(-1)?.[2] as string;
    expect(target).not.toMatch(/provider=/);
    expect(target).toMatch(/checkout=success/);
    expect(onComplete).not.toHaveBeenCalled();
    replaceStateSpy.mockRestore();
  });
});

describe("ChatGPTOAuthStep plan gate (free signup that picked ChatGPT)", () => {
  it("shows the plan step first and sends Checkout back to the onboarding step", async () => {
    sessionStorage.clear();
    billing.isSubscribed = false;
    searchParams = new URLSearchParams();
    try {
      render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
      fireEvent.click(screen.getByRole("button", { name: "Choose the Workspace plan" }));
      expect(billing.startCheckout).toHaveBeenCalledWith("workspace", false, "chatgpt_onboarding");
    } finally { billing.isSubscribed = true; }
  });

  it("mounted after the shell stripped ?checkout= (provider picker first): still waits, never sells the plan again", () => {
    billing.isSubscribed = false;
    searchParams = new URLSearchParams("provider=chatgpt_oauth"); // already stripped
    markCheckoutReturn();
    try {
      render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
      expect(screen.getByText("Confirming your plan…")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Choose the Workspace plan" })).toBeNull();
    } finally { billing.isSubscribed = true; searchParams = new URLSearchParams(); sessionStorage.clear(); }
  });

  it("back from Checkout with the webhook slow: never the plan step again; 'still being confirmed' with Check again", async () => {
    vi.useFakeTimers();
    billing.isSubscribed = false;
    searchParams = new URLSearchParams("provider=chatgpt_oauth&checkout=success");
    try {
      render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
      expect(screen.getByText("Confirming your plan…")).toBeInTheDocument();
      await act(async () => { vi.advanceTimersByTime(31_000); });
      expect(screen.getByText("Your payment is still being confirmed.")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Choose the Workspace plan" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Check again" }));
      expect(billing.refresh).toHaveBeenCalled();
      expect(screen.getByText("Confirming your plan…")).toBeInTheDocument();
    } finally { billing.isSubscribed = true; searchParams = new URLSearchParams(); vi.useRealTimers(); }
  });
});
