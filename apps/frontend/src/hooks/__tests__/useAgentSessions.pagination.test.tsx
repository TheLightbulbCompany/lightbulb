import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

// useAgentSessions reaches the gateway only through useGatewayRpcMutation
// after Task 5, so mocking that one export is the whole surface. SWR needs
// no provider — its default cache is module-global.
const sendReq = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({ data: undefined, isLoading: false, mutate: vi.fn() }),
  useGatewayRpcMutation: () => sendReq,
}));
vi.mock("@/lib/notify", () => ({
  notifyApiError: vi.fn(),
  notifyError: vi.fn(),
  notify: vi.fn(),
  notifySuccess: vi.fn(),
}));

import { useAgentSessions } from "../useAgentSessions";

beforeEach(() => {
  sendReq.mockReset();
});

const AGENT = "11111111-1111-4111-8111-111111111111";
const USER = "user_abc";
const uuid = (n: number) => `2222222${n}-2222-4222-8222-222222222222`;

describe("useAgentSessions pagination", () => {
  it("keeps paging until the container reports no more rows", async () => {
    // Page 1 is entirely foreign rows — the user's own sessions are on page 2.
    sendReq.mockResolvedValueOnce({
      sessions: [{ key: `agent:${AGENT}:paperclip:issue:1` }],
      totalCount: 2,
      nextOffset: 1,
      hasMore: true,
    });
    sendReq.mockResolvedValueOnce({
      sessions: [{ key: `agent:${AGENT}:${USER}:${uuid(1)}`, updatedAt: 5 }],
      totalCount: 2,
      hasMore: false,
    });

    const { result } = renderHook(() => useAgentSessions(AGENT, USER));

    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.sessions[0].sessionId).toBe(uuid(1));
    expect(sendReq.mock.calls[1][1].offset).toBe(1);
  });
});
