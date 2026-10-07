import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { ChatGPTOAuthStep } from "../ChatGPTOAuthStep";
import type { OAuthState } from "@/hooks/useChatGPTOAuth";

const startMock = vi.fn();
const harnessStartMock = vi.fn();

interface MockHookValue {
  state: OAuthState;
  start: () => void;
  disconnect: () => Promise<void>;
  probed: boolean;
}

const stateRef: { current: MockHookValue } = { current: initialState() };
// The step mounts the hook a second time for the coding-harness grant
// (basePath "/oauth/chatgpt/harness"). Default: probe resolved, not
// connected — the state a first-time user sees.
const harnessRef: { current: MockHookValue } = { current: harnessInitialState() };

function initialState(): MockHookValue {
  return { state: { status: "idle" }, start: startMock, disconnect: vi.fn(), probed: true };
}

function harnessInitialState(): MockHookValue {
  return { state: { status: "idle" }, start: harnessStartMock, disconnect: vi.fn(), probed: true };
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
    probed: true,
  };
}

function completedState(): MockHookValue {
  return {
    state: { status: "completed", accountId: "acct_x" },
    start: startMock,
    disconnect: vi.fn(),
    probed: true,
  };
}

vi.mock("@/hooks/useChatGPTOAuth", () => ({
  useChatGPTOAuth: (opts?: { basePath?: string }) =>
    opts?.basePath === "/oauth/chatgpt/harness" ? harnessRef.current : stateRef.current,
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
    harnessStartMock.mockReset();
    writeTextMock.mockClear();
    searchParams = new URLSearchParams();
    stateRef.current = initialState();
    harnessRef.current = harnessInitialState();
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

  it("provider completed + harness unconnected: offers the harness, no auto-advance", () => {
    stateRef.current = completedState();
    const onComplete = vi.fn();
    render(<ChatGPTOAuthStep onComplete={onComplete} />);
    expect(screen.getByText(/enable coding for your agents/i)).toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("Skip for now completes the step without starting a harness session", async () => {
    stateRef.current = completedState();
    const onComplete = vi.fn();
    render(<ChatGPTOAuthStep onComplete={onComplete} />);
    fireEvent.click(screen.getByRole("button", { name: /skip for now/i }));
    await waitFor(() => expect(onComplete).toHaveBeenCalled());
    expect(harnessStartMock).not.toHaveBeenCalled();
  });

  it("Enable coding starts the harness device-code session", () => {
    stateRef.current = completedState();
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /enable coding/i }));
    expect(harnessStartMock).toHaveBeenCalledTimes(1);
  });

  it("harness pending: shows the second code with a skip escape", () => {
    stateRef.current = completedState();
    harnessRef.current = {
      state: {
        status: "pending",
        userCode: "HARN-1234",
        verificationUri: "https://auth.openai.com/codex/device",
        expiresAt: Date.now() + 600_000,
      },
      start: harnessStartMock,
      disconnect: vi.fn(),
      probed: true,
    };
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    expect(screen.getByText("HARN-1234")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /skip for now/i })).toBeInTheDocument();
  });

  it("auto-advances when the harness is already connected (re-entry)", async () => {
    stateRef.current = completedState();
    harnessRef.current = {
      state: { status: "completed", accountId: "acc_h" },
      start: harnessStartMock,
      disconnect: vi.fn(),
      probed: true,
    };
    const onComplete = vi.fn();
    render(<ChatGPTOAuthStep onComplete={onComplete} />);
    await waitFor(() => expect(onComplete).toHaveBeenCalled());
    expect(screen.queryByText(/enable coding for your agents/i)).not.toBeInTheDocument();
  });

  it("holds a spinner (no offer, no advance) until the harness probe resolves", () => {
    stateRef.current = completedState();
    harnessRef.current = { ...harnessInitialState(), probed: false };
    const onComplete = vi.fn();
    render(<ChatGPTOAuthStep onComplete={onComplete} />);
    expect(onComplete).not.toHaveBeenCalled();
    expect(screen.queryByText(/enable coding for your agents/i)).not.toBeInTheDocument();
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
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    searchParams = new URLSearchParams({ provider: "chatgpt_oauth", checkout: "success" });
    stateRef.current = pendingState(Date.now() + 600_000);
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    fireEvent.click(
      screen.getByRole("button", { name: /cancel and choose a different provider/i }),
    );
    const target = replaceStateSpy.mock.calls.at(-1)?.[2] as string;
    expect(target).not.toMatch(/provider=/);
    expect(target).toMatch(/checkout=success/);
    replaceStateSpy.mockRestore();
  });
});
