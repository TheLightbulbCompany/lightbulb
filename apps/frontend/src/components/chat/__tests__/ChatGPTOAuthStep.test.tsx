import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ChatGPTOAuthStep } from "../ChatGPTOAuthStep";
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

vi.mock("@/hooks/useChatGPTOAuth", () => ({
  useChatGPTOAuth: () => stateRef.current,
}));

describe("ChatGPTOAuthStep", () => {
  beforeEach(() => {
    startMock.mockReset();
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
    stateRef.current = {
      state: {
        status: "pending",
        userCode: "ABCD-1234",
        verificationUri: "https://chatgpt.com/auth/device",
        expiresAt: Date.now() + 600_000,
      },
      start: startMock,
      disconnect: vi.fn(),
    };
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    expect(screen.getByText("ABCD-1234")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /chatgpt.com\/auth\/device/i }),
    ).toBeInTheDocument();
  });

  it("calls onComplete when state transitions to completed", async () => {
    stateRef.current = {
      state: { status: "completed", accountId: "acct_x" },
      start: startMock,
      disconnect: vi.fn(),
    };
    const onComplete = vi.fn();
    render(<ChatGPTOAuthStep onComplete={onComplete} />);
    await waitFor(() => expect(onComplete).toHaveBeenCalled());
  });
});
