import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/lib/notify", () => ({
  notifyApiError: vi.fn(),
  notifyError: vi.fn(),
  notify: vi.fn(),
  notifySuccess: vi.fn(),
}));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn().mockResolvedValue({ messages: [] });
const sendChat = vi.fn();
const send = vi.fn();

vi.mock("@/hooks/useGateway", () => ({
  useGateway: (): {
    isConnected: boolean;
    nodeConnected: boolean;
    error: null;
    reconnectAttempt: number;
    send: typeof send;
    sendReq: typeof sendReq;
    sendChat: typeof sendChat;
    onEvent: (h: (n: string, d: unknown) => void) => () => void;
    onChatMessage: (h: (m: ChatIncomingMessage) => void) => () => void;
    reconnect: () => void;
  } => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq,
    sendChat,
    onEvent: () => () => {},
    onChatMessage: (h) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

function dispatch(msg: ChatIncomingMessage): void {
  chatHandlers.forEach((h) => h(msg));
}

// A first accepted chunk adopts the observed run and flips isStreaming on
// (useAgentChat:919-950). So on a clean hook — with no optimistic sendMessage
// priming the gate — isStreaming is the honest signal for "this frame was
// accepted": it stays false when the demux guard drops the frame and flips
// true only when the frame belongs to the active session.
describe("useAgentChat — event demux by session_id", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({ messages: [] });
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("drops a chunk stamped with a different session_id", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    let sid = "";
    act(() => {
      sid = result.current.newSession();
    });
    const runId = crypto.randomUUID();

    // Frame for a DIFFERENT session (a background tab's run) must not paint —
    // dropped by the demux guard, so it never adopts the run nor streams.
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId,
        content: "leak",
        session_id: "99999999-9999-4999-8999-999999999999",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);

    // Frame for the ACTIVE session is accepted and flips the stream on.
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId,
        content: "mine",
        session_id: sid,
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);
  });

  it("drops an untagged General frame while a named session is active (General→named leak)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    act(() => {
      result.current.newSession();
    });
    const runId = crypto.randomUUID();

    // A legacy General run carries NO session_id. With a named session active,
    // a `"session_id" in msg` guard would fail-open and let this leak in; the
    // normalized guard drops it (undefined id == null != the active UUID).
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId,
        content: "general-leak",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);
  });

  it("General session (null) accepts frames with no session_id and drops 4-part frames", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    const runId = crypto.randomUUID();

    // No active session (General). A frame tagged for a real session must not
    // reach the General view.
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId,
        content: "other",
        session_id: "11111111-1111-4111-8111-111111111111",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);

    // Legacy frame (no session_id) belongs to General → accepted.
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId, content: "general" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);
  });
});
