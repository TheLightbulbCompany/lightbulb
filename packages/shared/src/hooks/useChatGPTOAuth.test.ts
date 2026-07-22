// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";

import { useChatGPTOAuth } from "./useChatGPTOAuth";
import { ApiError } from "../api/error";

// Framework-neutral counterpart of the web hook test
// (apps/frontend/src/hooks/__tests__/useChatGPTOAuth.test.tsx) — same
// behavior, but the API client is injected directly instead of going through
// a mocked global fetch + @clerk/nextjs.

function makeApi() {
  return { get: vi.fn(), post: vi.fn() };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("useChatGPTOAuth (shared)", () => {
  it("auto-starts on mount and reaches pending with the device code", async () => {
    const api = makeApi();
    api.get.mockResolvedValue({ connected: false });
    api.post.mockImplementation(async (endpoint: unknown) => {
      if (endpoint === "/oauth/chatgpt/start") {
        return {
          user_code: "ABCD-1234",
          verification_uri: "https://chatgpt.com/auth/device",
          expires_in: 900,
          interval: 5,
        };
      }
      // The poll never reports completion in this test, so state settles on
      // "pending" durably — nothing to race against.
      return { status: "pending" };
    });

    const { result } = renderHook(() => useChatGPTOAuth(api));

    await waitFor(() => expect(result.current.state.status).toBe("pending"));
    expect(result.current.state).toMatchObject({
      status: "pending",
      userCode: "ABCD-1234",
      verificationUri: "https://chatgpt.com/auth/device",
    });
  });

  it("polls through to completed once the backend reports completion", async () => {
    const api = makeApi();
    api.get.mockResolvedValue({ connected: false });
    api.post
      .mockResolvedValueOnce({
        user_code: "ABCD-1234",
        verification_uri: "https://chatgpt.com/auth/device",
        expires_in: 900,
        // Shrink the poll interval so the test doesn't wait on a real 5s timer.
        interval: 0.05,
      })
      .mockResolvedValueOnce({ status: "pending" })
      .mockResolvedValueOnce({ status: "completed", account_id: "acct_1" });

    const { result } = renderHook(() => useChatGPTOAuth(api));

    await waitFor(() => expect(result.current.state.status).toBe("completed"));
    expect(result.current.state).toMatchObject({
      status: "completed",
      accountId: "acct_1",
    });
    expect(api.post).toHaveBeenCalledWith("/oauth/chatgpt/poll", {});
  });

  it("surfaces a non-409 start failure as state.error", async () => {
    const api = makeApi();
    api.get.mockResolvedValue({ connected: false });
    api.post.mockRejectedValue(new Error("OpenAI device-code failed"));

    const { result } = renderHook(() => useChatGPTOAuth(api));

    await waitFor(() => expect(result.current.state.status).toBe("error"));
    expect(result.current.state).toMatchObject({
      message: "OpenAI device-code failed",
    });
  });

  it("treats a 409 on start (already-active session) as completed", async () => {
    const api = makeApi();
    api.get.mockResolvedValue({ connected: false });
    api.post.mockRejectedValue(new ApiError(409, { detail: "already active" }));

    const { result } = renderHook(() => useChatGPTOAuth(api));

    await waitFor(() => expect(result.current.state.status).toBe("completed"));
    expect(result.current.state).toMatchObject({ accountId: null });
  });
});
