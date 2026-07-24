/**
 * `container_waking` self-heal for useAgentChat.
 *
 * When a chat send lands on a trial container that has scaled to zero, the
 * backend answers with a benign error frame `{code:"container_waking"}` instead
 * of the misleading "No container provisioned. Please subscribe." copy. The hook
 * must:
 *   1. Render the message as CALM plain text — no errorCode (so MessageList does
 *      not upgrade it to a red ChatErrorCard) and no top-level error banner.
 *   2. Still release the busy gate, exactly like any other no-runId transport
 *      error.
 *
 * No manual `/container/status` SWR revalidation anymore (spec PR-9,
 * CLIENT-HOOK deleted the wake/unpark/container_waking manual-mutate trio):
 * the sibling ProvisioningStepper's `useProvisioningState` now shares the ONE
 * `useOnboardingStatus` bounded loop, which re-polls on its own within its
 * declared <=15s ceiling regardless of this hook poking it — see that hook's
 * module doc.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import type { ChatRenderItem, HistoryMessage } from "@/hooks/useAgentChat";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

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
    vi.resetModules();
  });

  it("renders a calm, code-less notice and releases the busy gate", async () => {
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

    // Calm rendering: the notice is plain text (no "Error: " prefix), carries
    // NO errorCode (so MessageList keeps it as markdown, not a red card), and
    // the top-level error banner stays clear.
    const msgs = assistantMessages(result.current.renderItems);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].errorCode).toBeUndefined();
    expect(msgs[0].content?.[0]).toMatchObject({ type: "text", text: WAKING_MESSAGE });

    // Busy gate released (no-runId transport terminal), same as no-container.
    expect(result.current.isStreaming).toBe(false);
  });

  it("an ordinary terminal error keeps the Error: prefix (not the calm waking copy)", async () => {
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

    // Ordinary error keeps the "Error: " prefix in the transcript bubble (there
    // is no top-level banner — an error must never swap the panel layout).
    const ordinary = assistantMessages(result.current.renderItems);
    expect(ordinary).toHaveLength(1);
    expect(ordinary[0].content?.[0]).toMatchObject({
      type: "text",
      text: "Error: No container provisioned. Please subscribe to start chatting.",
    });
  });

  it("handles repeated waking frames without erroring (no stuck loop)", async () => {
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

    const msgs = assistantMessages(result.current.renderItems);
    expect(msgs).toHaveLength(2);
    expect(msgs.every((m) => m.errorCode === undefined)).toBe(true);
  });
});
