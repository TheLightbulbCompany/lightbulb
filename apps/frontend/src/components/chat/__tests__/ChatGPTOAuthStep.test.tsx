import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ChatGPTOAuthStep } from "../ChatGPTOAuthStep";

const startMock = vi.fn();
const stateRef: { current: ReturnType<typeof initialState> } = { current: initialState() };

function initialState() {
  return { state: { status: "idle" } as const, start: startMock, disconnect: vi.fn() };
}

vi.mock("@/hooks/useChatGPTOAuth", () => ({
  useChatGPTOAuth: () => stateRef.current,
}));

describe("ChatGPTOAuthStep", () => {
  beforeEach(() => {
    startMock.mockReset();
    stateRef.current = initialState();
  });

  it("auto-starts the device-code request on mount (no Connect ChatGPT button)", () => {
    render(<ChatGPTOAuthStep onComplete={vi.fn()} />);
    expect(startMock).toHaveBeenCalledTimes(1);
    // Idle-state CTA must not exist anymore.
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
