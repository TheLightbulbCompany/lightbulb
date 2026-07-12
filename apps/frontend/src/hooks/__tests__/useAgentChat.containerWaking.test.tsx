/**
 * `container_waking` self-heal for useAgentChat.
 *
 * When a chat send lands on a trial container that has scaled to zero, the
 * backend answers with a benign error frame `{code:"container_waking"}` instead
 * of the misleading "No container provisioned. Please subscribe." copy. The hook
 * must:
 *   1. Fire exactly ONE revalidation of the `/container/status` SWR key (the key
 *      useProvisioningState owns) per waking frame, so the sibling
 *      ProvisioningStepper flips to its cold-start UI and the box wakes.
 *   2. Render the message as CALM plain text — no errorCode (so MessageList does
 *      not upgrade it to a red ChatErrorCard) and no top-level error banner.
 *   3. Still release the busy gate, exactly like any other no-runId transport
 *      error.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import type { ChatRenderItem, HistoryMessage } from "@/hooks/useAgentChat";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

// Spy on the shared SWR revalidation the hook triggers via useSWRConfig().
// Referenced inside the (lazily-run) vi.mock factory — safe because the factory
// only executes when "swr" is first imported (when useAgentChat is dynamically
// imported inside each test), well after this const initializes.
const mutate = vi.fn();
vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, useSWRConfig: () => ({ mutate }) };
});

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
let eventHandlers: Array<(name: string, data: unknown) => void> = [];
const sendReq = vi.fn().mockResolvedValue({});
const sendChat = vi.fn();
const send = vi.fn();

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq,
    sendChat,
    onEvent: (h: (name: string, data: unknown) => void) => {
      eventHandlers.push(h);
      return () => {
        eventHandlers = eventHandlers.filter((x) => x !== h);
      };
    },
    onChatMessage: (h: (msg: ChatIncomingMessage) => void) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

function dispatch(msg: ChatIncomingMessage) {
  chatHandlers.forEach((h) => h(msg));
}

const WAKING_MESSAGE = "Waking your agent — this takes about a minute.";

function assistantMessages(renderItems: ChatRenderItem[]): HistoryMessage[] {
  return renderItems
    .filter((i): i is Extract<ChatRenderItem, { kind: "message" }> => i.kind === "message")
    .map((i) => i.message)
    .filter((m) => m.role === "assistant");
}

describe("useAgentChat — container_waking self-heal", () => {
  beforeEach(() => {
    chatHandlers = [];
    eventHandlers = [];
    sendReq.mockReset().mockResolvedValue({});
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    mutate.mockReset();
    vi.resetModules();
  });

  it("revalidates /container/status exactly once and renders a calm, code-less notice", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("hello");
    });
    // The send opened the busy gate.
    expect(result.current.isStreaming).toBe(true);

    act(() =>
      dispatch({
        type: "error",
        agent_id: "agent-X",
        code: "container_waking",
        message: WAKING_MESSAGE,
      } as ChatIncomingMessage),
    );

    // Exactly one revalidation of the shared /container/status key.
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate).toHaveBeenCalledWith("/container/status");

    // Calm rendering: the notice is plain text (no "Error: " prefix), carries
    // NO errorCode (so MessageList keeps it as markdown, not a red card), and
    // the top-level error banner stays clear.
    const msgs = assistantMessages(result.current.renderItems);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].errorCode).toBeUndefined();
    expect(msgs[0].content?.[0]).toMatchObject({ type: "text", text: WAKING_MESSAGE });
    expect(result.current.error).toBeNull();

    // Busy gate released (no-runId transport terminal), same as no-container.
    expect(result.current.isStreaming).toBe(false);
  });

  it("does NOT revalidate for an ordinary terminal error", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("hello");
    });

    act(() =>
      dispatch({
        type: "error",
        agent_id: "agent-X",
        message: "No container provisioned. Please subscribe to start chatting.",
      } as ChatIncomingMessage),
    );

    expect(mutate).not.toHaveBeenCalled();
    // Ordinary error keeps the "Error: " prefix + surfaces the top-level banner.
    expect(result.current.error).toBe(
      "No container provisioned. Please subscribe to start chatting.",
    );
  });

  it("fires exactly one revalidation per waking frame (no stuck loop)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));

    await act(async () => {
      await result.current.sendMessage("hello");
    });

    const waking = {
      type: "error",
      agent_id: "agent-X",
      code: "container_waking",
      message: WAKING_MESSAGE,
    } as ChatIncomingMessage;

    act(() => dispatch(waking));
    act(() => dispatch(waking));

    expect(mutate).toHaveBeenCalledTimes(2);
    expect(mutate).toHaveBeenNthCalledWith(1, "/container/status");
    expect(mutate).toHaveBeenNthCalledWith(2, "/container/status");
  });
});
