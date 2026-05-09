import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useChatGPTOAuth } from "../useChatGPTOAuth";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
);

describe("useChatGPTOAuth.start", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("transitions to completed when /oauth/chatgpt/start returns 409 (already-active session)", async () => {
    // Backend returns 409 (OAuthAlreadyActiveError) when an active token row
    // already exists for the user. The auto-fire from ChatGPTOAuthStep on a
    // stale ?provider=chatgpt_oauth URL (e.g. browser back/history restore)
    // hits this. Treating it as completion lets ProvisioningStepper advance
    // out of the wizard and strip ?provider= via providerStepDone — instead
    // of dead-ending the user on the "User already has active OAuth tokens"
    // error message.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: "User user_xyz already has active OAuth tokens — revoke before starting again",
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useChatGPTOAuth(), { wrapper });
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.state.status).toBe("completed");
  });

  it("surfaces non-409 errors as state.error", async () => {
    // 502 (OAuthExchangeFailedError) and other non-409 failures must not
    // be silently treated as completion — they're real failures the user
    // needs to see and retry.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "OpenAI device-code failed" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { result } = renderHook(() => useChatGPTOAuth(), { wrapper });
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.state.status).toBe("error");
  });

  it("transitions to pending on a normal 200 device-code response", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          user_code: "ABCD-1234",
          verification_uri: "https://chatgpt.com/auth/device",
          expires_in: 900,
          interval: 5,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useChatGPTOAuth(), { wrapper });
    await act(async () => {
      await result.current.start();
    });
    await waitFor(() => expect(result.current.state.status).toBe("pending"));
    if (result.current.state.status === "pending") {
      expect(result.current.state.userCode).toBe("ABCD-1234");
    }
  });
});
