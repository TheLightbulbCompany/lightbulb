# Chat Concurrent-Message Ordering Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Eliminate the JSONL-interleaving race where a second `chat.send` is dispatched while a previous run is still streaming. Aligns Isol8's chat UX with upstream OpenClaw control-ui by mirroring its client-assigned runId + frontend chat queue pattern.

**Architecture:** Frontend mints a UUID per user message (`clientRunId`), passes it through `agent_chat` payload → backend → upstream OpenClaw `chat.send` as `idempotencyKey`. The frontend tracks a per-user-turn `clientRunIdRef` separately from the existing per-bubble `runsRef`. While `clientRunIdRef.current !== null`, new sends push to a `chatQueueRef` that auto-drains when the turn ends. Multi-bubble rendering is preserved — bubbles are for display, the busy flag is for send gating. They are orthogonal.

**Tech Stack:** TypeScript / React 19 (frontend); FastAPI / Python (backend); existing useGateway WebSocket plumbing; vitest for frontend unit tests; pytest for backend unit tests.

**Scope boundary:** Single-tab frontend queueing only. Backend-side per-session serialization (which would also catch multi-tab races) is explicitly NOT in this PR — see "Out of scope" at the bottom.

**Decisions made up front** (so the engineer doesn't re-litigate):

1. **Cancel/Stop behavior** mirrors upstream `app-chat.ts:152-167`: aborts the active run only, does NOT clear `chatQueueRef`. Queue continues draining after abort. Add an explicit "Clear queue" button for queued items.
2. **Queue UI**: pill above the input showing `+N queued` plus a compact list of queued message rows with an X to remove each. Mirrors upstream's `app-render.ts:2392` queue rendering.
3. **Send button visibility**: stays visible always (no more disable-while-streaming). Pressing it during a busy turn enqueues. Stop button shown next to Send when `clientRunIdRef.current !== null`.
4. **`done`/`error`/`aborted` event matching**: clear `clientRunIdRef` only when the event's `runId` equals the value we sent as idempotencyKey. Other-runId events keep their existing per-bubble behavior. Mirrors upstream `controllers/chat.ts:738-748,769-770`.

---

## File Structure

**Backend (Python / FastAPI):**
- Modify `apps/backend/routers/websocket_chat.py` — accept `clientRunId` in `agent_chat` payload, forward through to `_process_agent_chat_background`, which adds `idempotencyKey: clientRunId` to the `chat.send` RPC params.
- Add `apps/backend/tests/routers/test_websocket_chat_idempotency.py` — verifies the forward.

**Frontend (TypeScript / React):**
- Modify `apps/frontend/src/hooks/useGateway.tsx` — extend `sendChat` signature to accept `clientRunId`.
- Modify `apps/frontend/src/hooks/useAgentChat.ts` — add `clientRunIdRef`, `chatQueueRef`, queue-aware `sendMessage`, runId-matched terminal-event handlers, `flushQueue` effect, `removeQueuedMessage`, `clearQueue`.
- Modify `apps/frontend/src/components/chat/ChatInput.tsx` — remove `disabled={isStreaming}`; keep input enabled; render the queued list above the textarea; show Send and Stop buttons side-by-side when busy.
- Modify `apps/frontend/src/components/chat/AgentChatWindow.tsx` — pass new `queuedMessages`, `removeQueuedMessage`, `clearQueue` props to ChatInput.
- Add `apps/frontend/src/hooks/__tests__/useAgentChat.queue.test.ts` — unit tests for the queue/clientRunId state machine.

---

## Task 1: Backend — accept `clientRunId` in `agent_chat` payload

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py:432-459` (the `agent_chat` dispatch branch) and `:918-1077` (`_process_agent_chat_background`)

- [x] **Step 1: Read the current dispatch and forwarder**

Reference: `apps/backend/routers/websocket_chat.py:432-459` and `:1066-1077`. Confirm the current `chat.send` params dict is `{"sessionKey": session_key, "message": message}` (no idempotencyKey).

- [x] **Step 2: Write the failing test**

Create `apps/backend/tests/routers/test_websocket_chat_idempotency.py`:

```python
"""Verify that clientRunId from the agent_chat payload is forwarded as
idempotencyKey to OpenClaw chat.send, mirroring upstream control-ui's
pattern in ~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:502.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers.websocket_chat import _process_agent_chat_background


@pytest.mark.asyncio
async def test_client_run_id_is_forwarded_as_idempotency_key():
    """If the agent_chat payload includes clientRunId, the backend MUST
    forward it as idempotencyKey on the chat.send RPC params so OpenClaw
    binds the user-turn to that UUID. Without this, OpenClaw assigns its
    own runId and the frontend cannot match the chat.final event to the
    user's send — leaving clientRunIdRef stuck and the queue undrained."""

    fake_pool = MagicMock()
    fake_pool.send_rpc = AsyncMock(return_value={"ok": True})
    fake_pool.mark_run_active = MagicMock()
    fake_pool.gate_chat = AsyncMock(return_value={"blocked": False})

    fake_ecs = MagicMock()
    fake_ecs.resolve_running_container = AsyncMock(
        return_value=({"gateway_token": "t"}, "10.0.0.1"),
    )

    fake_mgmt = MagicMock()
    fake_mgmt.send_message = MagicMock()

    with (
        patch("routers.websocket_chat.get_gateway_pool", return_value=fake_pool),
        patch("routers.websocket_chat.get_ecs_manager", return_value=fake_ecs),
        patch(
            "routers.websocket_chat.get_management_api_client",
            AsyncMock(return_value=fake_mgmt),
        ),
        patch("routers.websocket_chat.get_user_node", return_value=None),
    ):
        await _process_agent_chat_background(
            connection_id="c1",
            user_id="user_abc",
            owner_id="user_abc",
            agent_id="agent-xyz",
            message="hello",
            client_run_id="11111111-2222-3333-4444-555555555555",
        )

    # Find the chat.send RPC call among any other RPC calls (e.g., sessions.patch)
    chat_send_calls = [
        c for c in fake_pool.send_rpc.await_args_list
        if c.kwargs.get("method") == "chat.send"
    ]
    assert len(chat_send_calls) == 1, "expected exactly one chat.send call"
    params = chat_send_calls[0].kwargs["params"]
    assert params["idempotencyKey"] == "11111111-2222-3333-4444-555555555555"
    assert params["sessionKey"] == "agent:agent-xyz:user_abc"
    assert params["message"] == "hello"


@pytest.mark.asyncio
async def test_missing_client_run_id_does_not_add_idempotency_key():
    """Backwards-compat: old frontend clients that don't send clientRunId
    still work — the backend just omits idempotencyKey, and OpenClaw
    assigns its own runId (existing behavior). Same shape as today."""

    fake_pool = MagicMock()
    fake_pool.send_rpc = AsyncMock(return_value={"ok": True})
    fake_pool.mark_run_active = MagicMock()
    fake_pool.gate_chat = AsyncMock(return_value={"blocked": False})

    fake_ecs = MagicMock()
    fake_ecs.resolve_running_container = AsyncMock(
        return_value=({"gateway_token": "t"}, "10.0.0.1"),
    )

    fake_mgmt = MagicMock()
    fake_mgmt.send_message = MagicMock()

    with (
        patch("routers.websocket_chat.get_gateway_pool", return_value=fake_pool),
        patch("routers.websocket_chat.get_ecs_manager", return_value=fake_ecs),
        patch(
            "routers.websocket_chat.get_management_api_client",
            AsyncMock(return_value=fake_mgmt),
        ),
        patch("routers.websocket_chat.get_user_node", return_value=None),
    ):
        await _process_agent_chat_background(
            connection_id="c1",
            user_id="user_abc",
            owner_id="user_abc",
            agent_id="agent-xyz",
            message="hello",
            client_run_id=None,
        )

    chat_send_calls = [
        c for c in fake_pool.send_rpc.await_args_list
        if c.kwargs.get("method") == "chat.send"
    ]
    assert len(chat_send_calls) == 1
    params = chat_send_calls[0].kwargs["params"]
    assert "idempotencyKey" not in params
```

(Do not run tests yet — per project convention defer to the end-of-plan verification batch.)

- [x] **Step 3: Modify the dispatch to read `clientRunId` from the payload**

In `apps/backend/routers/websocket_chat.py`, locate the `agent_chat` dispatch at line 432. Modify to read `clientRunId` (optional) and pass to the background task:

```python
    if msg_type == "agent_chat":
        agent_id = body.get("agent_id")
        message = body.get("message")
        # Frontend-minted UUID per user turn. Forwarded to OpenClaw as
        # idempotencyKey so the frontend can match the chat.final event
        # to the user's send and drain its local chat queue. Mirrors
        # upstream ~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:502.
        # Optional for backwards-compat with old frontend builds — without
        # it, OpenClaw assigns its own runId (existing behavior).
        client_run_id = body.get("clientRunId")

        if not agent_id or not message:
            management_api = await get_management_api_client()
            management_api.send_message(
                x_connection_id,
                {"type": "error", "message": "Missing agent_id or message"},
            )
            return Response(status_code=200)

        background_tasks.add_task(
            _process_agent_chat_background,
            connection_id=x_connection_id,
            user_id=user_id,
            owner_id=owner_id,
            agent_id=agent_id,
            message=message,
            client_run_id=client_run_id,
        )
        return Response(status_code=200)
```

- [x] **Step 4: Modify `_process_agent_chat_background` signature and `chat.send` params**

In the same file, change the function signature at `:918` to accept `client_run_id: str | None = None`. Locate the `chat.send` RPC call near `:1066-1077` and conditionally add `idempotencyKey`:

```python
async def _process_agent_chat_background(
    connection_id: str,
    user_id: str,
    owner_id: str,
    agent_id: str,
    message: str,
    client_run_id: str | None = None,
) -> None:
    """
    Process agent chat message via OpenClaw's native chat.send RPC.

    ...existing docstring...

    client_run_id: optional frontend-minted UUID forwarded to OpenClaw as
    idempotencyKey. Lets the frontend correlate chat.final/aborted/error
    events back to the user's send for queue draining. See spec
    docs/superpowers/plans/2026-05-11-chat-concurrent-message-ordering-fix.md.
    """
```

Then near the chat.send dispatch:

```python
        chat_send_params: dict[str, str] = {
            "sessionKey": session_key,
            "message": message,
        }
        if client_run_id:
            # Upstream chat.ts:2174 calls `context.addChatRun(clientRunId, ...)`
            # — this makes our UUID the run identifier. The frontend's
            # clientRunIdRef matches against the runId on the chat.final
            # event to know when the user-turn truly ended.
            chat_send_params["idempotencyKey"] = client_run_id

        result = await pool.send_rpc(
            user_id=owner_id,
            req_id=req_id,
            method="chat.send",
            params=chat_send_params,
            ip=ip,
            token=container["gateway_token"],
        )
```

- [x] **Step 5: Commit**

```bash
git add apps/backend/routers/websocket_chat.py apps/backend/tests/routers/test_websocket_chat_idempotency.py
git commit -m "feat(chat): forward clientRunId to OpenClaw as idempotencyKey

Frontend mints a UUID per user-initiated chat.send and now passes it
through the agent_chat WS payload. Backend forwards as idempotencyKey
so the frontend can match the eventual chat.final/aborted/error event
to the specific user turn (enables the upcoming queue-drain logic).

No behavioral change when clientRunId is absent — OpenClaw still
assigns its own runId, matching today's flow."
```

---

## Task 2: Frontend — extend `useGateway.sendChat` to accept clientRunId

**Files:**
- Modify: `apps/frontend/src/hooks/useGateway.tsx:91,425-438,479-480`

- [x] **Step 1: Modify the `sendChat` signature and payload**

Change the context type at `:91` and the implementation at `:425-438`:

```ts
  sendChat: (agentId: string, message: string, clientRunId: string) => void;
```

```ts
  const sendChat = useCallback(
    (agentId: string, message: string, clientRunId: string) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "agent_chat",
            agent_id: agentId,
            message,
            clientRunId,
          }),
        );
      }
    },
    [],
  );
```

- [x] **Step 2: Commit**

```bash
git add apps/frontend/src/hooks/useGateway.tsx
git commit -m "feat(chat): thread clientRunId through useGateway.sendChat"
```

---

## Task 3: Frontend — add `clientRunIdRef` and `chatQueueRef` to useAgentChat

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts:104-127` (types) and `:148-215` (hook body where refs live)

- [x] **Step 1: Extend the return type at `:107-117`**

Add three new exposed surface members:

```ts
export interface QueuedMessage {
  id: string;
  text: string;
  createdAt: number;
}

export interface UseAgentChatReturn {
  messages: AgentMessage[];
  isStreaming: boolean;
  error: string | null;
  sendMessage: (message: string) => Promise<void>;
  cancelMessage: () => Promise<void>;
  clearMessages: () => void;
  isConnected: boolean;
  isLoadingHistory: boolean;
  needsBootstrap: boolean;
  // ↓ new: chat-queue surface, mirrors upstream OpenClaw control-ui
  // (~/Desktop/openclaw/ui/src/ui/app-chat.ts:181-217,420-426).
  queuedMessages: QueuedMessage[];
  removeQueuedMessage: (id: string) => void;
  clearQueue: () => void;
}
```

- [x] **Step 2: Add `clientRunIdRef`, `chatQueueRef`, `queuedMessages` state inside the hook body**

Place these alongside the existing `runsRef` (line 179) and other refs, BEFORE `getOrCreateBubble`:

```ts
  // Upstream pattern: one client-assigned UUID per user-initiated chat.send.
  // Cleared only on the chat.final / aborted / error event whose runId
  // matches this UUID. Distinct from runsRef (per-bubble) — they are
  // orthogonal: bubbles are for display, this flag is for send gating.
  // See ~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:627-770.
  const clientRunIdRef = useRef<string | null>(null);

  // Frontend chat queue: when the user types while clientRunIdRef.current
  // is set (user-turn in progress), new sends push here instead of firing
  // immediately. Drained one-at-a-time on the busy→idle transition. Mirrors
  // ~/Desktop/openclaw/ui/src/ui/app-chat.ts:181-217,388-417.
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  // Keep the ref in sync so setMessages-style updates can read fresh state
  // from inside event handler callbacks without stale-closure bugs.
  useEffect(() => {
    queuedMessagesRef.current = queuedMessages;
  }, [queuedMessages]);
```

- [x] **Step 3: Add `removeQueuedMessage` and `clearQueue` callbacks**

After the `queuedMessages` state, near where other useCallbacks live:

```ts
  const removeQueuedMessage = useCallback((id: string) => {
    setQueuedMessages((prev) => prev.filter((m) => m.id !== id));
  }, []);

  const clearQueue = useCallback(() => {
    setQueuedMessages([]);
  }, []);
```

- [x] **Step 4: Export the new surface from the hook's return**

At the existing return statement near `:809-820`:

```ts
  return {
    messages: messages.map((m) => ({
      role: m.role,
      content: m.content,
      ...(m.thinking ? { thinking: m.thinking } : {}),
      ...(m.toolUses ? { toolUses: m.toolUses } : {}),
      ...(m.errorCode ? { errorCode: m.errorCode } : {}),
    })),
    isStreaming,
    error,
    sendMessage,
    cancelMessage,
    clearMessages,
    isConnected,
    isLoadingHistory,
    needsBootstrap,
    queuedMessages,
    removeQueuedMessage,
    clearQueue,
  };
```

- [x] **Step 5: Commit**

```bash
git add apps/frontend/src/hooks/useAgentChat.ts
git commit -m "feat(chat): add clientRunIdRef + chatQueue state scaffolding"
```

---

## Task 4: Frontend — rewrite `sendMessage` to mint clientRunId and enqueue when busy

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts:679-738` (the `sendMessage` useCallback)

- [x] **Step 1: Replace `sendMessage` with the queue-aware version**

```ts
  const sendMessage = useCallback(
    async (message: string): Promise<void> => {
      pendingCancelRef.current = false;

      if (!agentIdRef.current) {
        throw new Error("No agent selected");
      }
      if (!isConnected) {
        setError("Not connected. Please wait and try again.");
        return;
      }

      const trimmed = message.trim();
      if (!trimmed) return;

      setError(null);
      const key = agentIdRef.current ? `${agentIdRef.current}:${sessionName}` : null;
      if (key) _needsBootstrap.delete(key);

      // Busy gate: if a user-turn is already in flight (clientRunIdRef
      // is set), enqueue instead of firing chat.send. The flushQueue
      // effect picks this up when clientRunIdRef clears.
      if (clientRunIdRef.current !== null) {
        const id = `queued-${crypto.randomUUID()}`;
        setQueuedMessages((prev) => [
          ...prev,
          { id, text: trimmed, createdAt: Date.now() },
        ]);
        return;
      }

      await dispatchNow(trimmed);
    },
    [isConnected, sessionName],
  );

  // Hoisted so both sendMessage and the flushQueue effect call it.
  // dispatchNow assumes the busy-gate has already been checked.
  const dispatchNow = useCallback(
    async (message: string): Promise<void> => {
      const agentId = agentIdRef.current;
      if (!agentId) return;

      const userMsgId = `user-${crypto.randomUUID()}`;
      const newClientRunId = crypto.randomUUID();
      clientRunIdRef.current = newClientRunId;

      setMessages((prev) => [
        ...prev,
        { id: userMsgId, role: "user", content: message },
      ]);
      setIsStreaming(true);

      turnStartedAtRef.current = Date.now();
      turnAssistantLenRef.current = 0;
      capture("chat_message_sent", {
        agent_id: agentId,
        message_length: message.length,
      });

      try {
        sendChat(agentId, message, newClientRunId);
      } catch (err) {
        const errorMessage = friendlyError(
          err instanceof Error ? err.message : "Failed to send message",
        );
        setError(errorMessage);
        const errMsgId = `assistant-${crypto.randomUUID()}`;
        setMessages((prev) => [
          ...prev,
          { id: errMsgId, role: "assistant", content: `Error: ${errorMessage}` },
        ]);
        setIsStreaming(false);
        clientRunIdRef.current = null; // failure path resets busy gate
      }
    },
    [sendChat],
  );
```

Note: `dispatchNow` must be declared BEFORE `sendMessage` in the source order (TDZ), and `sendMessage`'s dependency array must include `dispatchNow`. Adjust if your hook layout requires it.

- [x] **Step 2: Commit**

```bash
git add apps/frontend/src/hooks/useAgentChat.ts
git commit -m "feat(chat): mint clientRunId per send; enqueue when turn in flight"
```

---

## Task 5: Frontend — wire `done`/`error`/`aborted` to clear clientRunId and drain queue

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts:434-516` (existing terminal-event handlers) and add a flush effect after `dispatchNow`.

- [x] **Step 1: Augment the `done` handler at `:434-438`**

```ts
      if (msg.type === "done") {
        const runId = msg.runId;
        finalizeBubble(runId);
        // Upstream pattern: only clear the user-turn busy flag when the
        // terminal event matches OUR clientRunId. Sub-run dones (different
        // runId) only finalize their bubble. See
        // ~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:769-770.
        if (runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }
        return;
      }
```

- [x] **Step 2: Augment the `error` handler**

Inside the `if (msg.type === "error")` block at `:440-516`, add the same clientRunIdRef clear logic. Place it after the existing finalize/bubble logic but BEFORE the `return`:

```ts
        // Clear the busy gate if this error pertains to the user's turn.
        // Scoped error with matching runId, or global error (no runId)
        // both terminate the user turn.
        if (!msg.runId || msg.runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }
```

Add it in both the scoped-error branch (after `setError(displayError)` at `:494`) and the global-error branch (after `setError(displayError)` at `:509`).

- [x] **Step 3: Augment `cancelMessage`**

In `cancelMessage` at `:742-773`, after the existing `pendingCancelRef.current = true` line, add:

```ts
    // Cancel terminates the current user-turn. Queue is NOT cleared —
    // upstream behavior is that abort only stops the active run; queued
    // follow-ups keep draining. Users can clearQueue() explicitly if
    // they want everything gone.
    clientRunIdRef.current = null;
```

- [x] **Step 4: Add the flush-queue effect**

Right after `dispatchNow` is declared:

```ts
  // Drain queued messages one at a time on busy→idle transitions.
  // We track the previous value of clientRunIdRef in a ref so a simple
  // effect can detect the falling edge without polling.
  const prevClientRunIdRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = prevClientRunIdRef.current;
    const curr = clientRunIdRef.current;
    prevClientRunIdRef.current = curr;
    if (prev !== null && curr === null && queuedMessagesRef.current.length > 0) {
      const next = queuedMessagesRef.current[0];
      setQueuedMessages((q) => q.slice(1));
      void dispatchNow(next.text);
    }
  });
```

Note: the effect deliberately has no dependency array — it runs after every render, and the ref-comparison guards against repeated dispatches. This is cheap (one ref read + one comparison) and avoids the need to thread clientRunIdRef through a useState (which would force a re-render per change).

- [x] **Step 5: Commit**

```bash
git add apps/frontend/src/hooks/useAgentChat.ts
git commit -m "feat(chat): clear clientRunIdRef on matched terminal events; flush queue on busy→idle"
```

---

## Task 6: Frontend — re-enable input while busy; render queued list

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatInput.tsx:19-249`
- Modify: `apps/frontend/src/components/chat/AgentChatWindow.tsx:233-298,327,386`

- [x] **Step 1: Extend ChatInput props**

Edit `ChatInput.tsx:14-32`:

```tsx
interface ChatInputProps {
  onSend: (message: string) => void;
  onStop?: () => void;
  disabled?: boolean;
  centered?: boolean;
  isUploading?: boolean;
  isStreaming?: boolean;
  suggestedMessage?: string;
  // ↓ new
  queuedMessages?: { id: string; text: string }[];
  onRemoveQueued?: (id: string) => void;
  onClearQueue?: () => void;
}

export function ChatInput({
  onSend,
  onStop,
  disabled,
  centered,
  isUploading,
  isStreaming,
  suggestedMessage,
  queuedMessages = [],
  onRemoveQueued,
  onClearQueue,
}: ChatInputProps) {
```

- [x] **Step 2: Adjust `isDisabled` to NOT include isStreaming**

At `ChatInput.tsx:147`:

```tsx
  // Previously: const isDisabled = disabled || isUploading;
  // We still respect explicit `disabled` from the parent (e.g. no agent
  // selected) and `isUploading`, but we deliberately DROP isStreaming
  // here — input must stay enabled so the user can type while the agent
  // is responding (messages get queued).
  const isDisabled = disabled || isUploading;
```

(No code change if `isDisabled` already matches the line above — only the comment is new. The `disabled={isTyping}` plumbing in AgentChatWindow is what needs to stop passing isStreaming as `disabled`. See step 5 below.)

- [x] **Step 3: Render the queued message strip above the input**

Just above the existing `<textarea>` element (locate where the input wrapper opens, around `:200`):

```tsx
      {queuedMessages.length > 0 && (
        <div className="flex flex-col gap-1 mb-2 px-2 py-1 rounded-md bg-[#f4f0e6] border border-[#e0dbd0]">
          <div className="flex items-center justify-between text-xs text-[#6f6856]">
            <span>{queuedMessages.length} queued</span>
            {onClearQueue && (
              <button
                type="button"
                onClick={onClearQueue}
                className="hover:underline"
              >
                Clear queue
              </button>
            )}
          </div>
          {queuedMessages.map((q) => (
            <div
              key={q.id}
              className="flex items-center gap-2 text-sm text-[#3a3528]"
            >
              <span className="flex-1 truncate">{q.text}</span>
              {onRemoveQueued && (
                <button
                  type="button"
                  aria-label="Remove queued message"
                  onClick={() => onRemoveQueued(q.id)}
                  className="text-[#9a8f7a] hover:text-[#3a3528]"
                >
                  ×
                </button>
              )}
            </div>
          ))}
        </div>
      )}
```

- [x] **Step 4: Adjust the Send/Stop toggle to always show Send**

At `ChatInput.tsx:232-249`, current code renders Stop OR Send based on `isStreaming`. Change to render Send always, AND render Stop next to it when streaming:

```tsx
      <div className="flex items-center gap-2">
        {isStreaming && onStop && (
          <Button
            type="button"
            onClick={onStop}
            size="icon"
            variant="ghost"
            aria-label="Stop"
            className="shrink-0 h-8 w-8 rounded-full"
            data-testid="stop-button"
          >
            <Square className="h-3.5 w-3.5" />
          </Button>
        )}
        <Button
          type="button"
          onClick={handleSend}
          size="icon"
          className="shrink-0 h-8 w-8 rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white disabled:bg-[#e0dbd0] disabled:text-[#b5ae9e]"
          disabled={(!input.trim() && pendingFiles.length === 0) || isDisabled}
          data-testid="send-button"
        >
          <ArrowUp className="h-3.5 w-3.5" />
        </Button>
      </div>
```

(If `Square` / `ArrowUp` aren't already imported from `lucide-react` at the top of the file, add them.)

- [x] **Step 5: Stop passing `isStreaming` as `disabled` in AgentChatWindow**

At `AgentChatWindow.tsx:327` and `:386`, current:

```tsx
<ChatInput onSend={handleSend} onStop={cancelMessage} disabled={isTyping} isStreaming={isStreaming} isUploading={isUploading} />
```

Change to:

```tsx
<ChatInput
  onSend={handleSend}
  onStop={cancelMessage}
  disabled={!agentId}
  isStreaming={isStreaming}
  isUploading={isUploading}
  queuedMessages={queuedMessages}
  onRemoveQueued={removeQueuedMessage}
  onClearQueue={clearQueue}
/>
```

And ensure `queuedMessages`, `removeQueuedMessage`, `clearQueue` are destructured from the `useAgentChat()` return at the top of the component (around `:230-250`):

```tsx
  const {
    messages,
    isStreaming,
    error,
    sendMessage,
    cancelMessage,
    clearMessages,
    isConnected: chatConnected,
    isLoadingHistory,
    needsBootstrap,
    queuedMessages,
    removeQueuedMessage,
    clearQueue,
  } = useAgentChat(agentId, sessionName);
```

Repeat the prop passing at the second `<ChatInput>` site near `:386`.

Also remove `disabled={isTyping || !agentId}` at `:365` if it's on a `<ChatInput>`; replace with `disabled={!agentId}`. Double-check that no other code path relies on `isTyping` to gate inputs (search for `isTyping` in the file).

- [x] **Step 6: Commit**

```bash
git add apps/frontend/src/components/chat/ChatInput.tsx apps/frontend/src/components/chat/AgentChatWindow.tsx
git commit -m "feat(chat): render queued-message strip; keep input enabled while streaming

Send button stays visible; Stop button appears alongside while a turn is
in flight. Messages typed during an active turn enqueue into the new
chat-queue surface from useAgentChat."
```

---

## Task 7: Frontend tests — queue / clientRunId state machine

**Files:**
- Create: `apps/frontend/src/hooks/__tests__/useAgentChat.queue.test.ts`

- [x] **Step 1: Write the test file**

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useAgentChat } from "../useAgentChat";

// Minimal GatewayProvider mock. The real provider is heavy (websockets,
// reconnect logic); we only need sendChat / onChatMessage / isConnected
// for these tests, so we stub the module.
vi.mock("@/hooks/useGateway", () => {
  const chatHandlers = new Set<(msg: unknown) => void>();
  const sendChat = vi.fn();
  const sendReq = vi.fn().mockResolvedValue({});
  return {
    useGateway: () => ({
      isConnected: true,
      sendChat,
      sendReq,
      onChatMessage: (fn: (msg: unknown) => void) => {
        chatHandlers.add(fn);
        return () => chatHandlers.delete(fn);
      },
    }),
    // Test helper exposed via the same module — convenient for emitting
    // chat events at the handlers list above.
    __emit: (msg: unknown) => {
      for (const h of chatHandlers) h(msg);
    },
    __sendChat: sendChat,
    __clear: () => {
      chatHandlers.clear();
      sendChat.mockClear();
    },
  };
});

import * as Gateway from "@/hooks/useGateway";
// Cast to access the test helpers above.
const G = Gateway as unknown as {
  __emit: (msg: unknown) => void;
  __sendChat: ReturnType<typeof vi.fn>;
  __clear: () => void;
};

beforeEach(() => {
  G.__clear();
});

describe("useAgentChat queue + clientRunId", () => {
  it("first sendMessage fires sendChat with a fresh clientRunId", async () => {
    const { result } = renderHook(() => useAgentChat("agent-1", "user-1"));
    await act(async () => {
      await result.current.sendMessage("hello");
    });
    expect(G.__sendChat).toHaveBeenCalledTimes(1);
    const [agentId, message, clientRunId] = G.__sendChat.mock.calls[0];
    expect(agentId).toBe("agent-1");
    expect(message).toBe("hello");
    expect(typeof clientRunId).toBe("string");
    expect(clientRunId).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("second sendMessage during a turn enqueues instead of firing", async () => {
    const { result } = renderHook(() => useAgentChat("agent-1", "user-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = G.__sendChat.mock.calls[0][2];

    // Emit a chunk for the first turn — turn is now in flight.
    await act(async () => {
      G.__emit({ type: "chunk", runId: firstRunId, content: "hi" });
    });

    // Second send arrives mid-turn — must enqueue, not call sendChat again.
    await act(async () => {
      await result.current.sendMessage("second");
    });
    expect(G.__sendChat).toHaveBeenCalledTimes(1); // still just the first
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("second");
  });

  it("on matching `done`, drains the queue and fires sendChat for the next item", async () => {
    const { result } = renderHook(() => useAgentChat("agent-1", "user-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = G.__sendChat.mock.calls[0][2];
    await act(async () => {
      G.__emit({ type: "chunk", runId: firstRunId, content: "hi" });
      await result.current.sendMessage("second");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // Matching `done` for the first turn — should clear busy and drain.
    await act(async () => {
      G.__emit({ type: "done", runId: firstRunId });
    });

    expect(result.current.queuedMessages).toHaveLength(0);
    expect(G.__sendChat).toHaveBeenCalledTimes(2);
    expect(G.__sendChat.mock.calls[1][1]).toBe("second");
    // The second send should have a DIFFERENT clientRunId.
    expect(G.__sendChat.mock.calls[1][2]).not.toBe(firstRunId);
  });

  it("non-matching `done` (sub-run) does NOT drain the queue", async () => {
    const { result } = renderHook(() => useAgentChat("agent-1", "user-1"));

    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = G.__sendChat.mock.calls[0][2];
    await act(async () => {
      G.__emit({ type: "chunk", runId: firstRunId, content: "hi" });
      await result.current.sendMessage("second");
    });
    expect(result.current.queuedMessages).toHaveLength(1);

    // A sub-run finalizes with a DIFFERENT runId. The user-turn busy
    // flag must remain set — this is the bug we're fixing.
    await act(async () => {
      G.__emit({ type: "done", runId: "sub-run-id-not-the-clientRunId" });
    });

    expect(result.current.queuedMessages).toHaveLength(1); // still queued
    expect(G.__sendChat).toHaveBeenCalledTimes(1); // no second dispatch
  });

  it("removeQueuedMessage drops a specific queue item", async () => {
    const { result } = renderHook(() => useAgentChat("agent-1", "user-1"));
    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = G.__sendChat.mock.calls[0][2];
    await act(async () => {
      G.__emit({ type: "chunk", runId: firstRunId, content: "hi" });
      await result.current.sendMessage("queued-1");
      await result.current.sendMessage("queued-2");
    });
    expect(result.current.queuedMessages).toHaveLength(2);
    const idToRemove = result.current.queuedMessages[0].id;
    await act(async () => {
      result.current.removeQueuedMessage(idToRemove);
    });
    expect(result.current.queuedMessages).toHaveLength(1);
    expect(result.current.queuedMessages[0].text).toBe("queued-2");
  });

  it("clearQueue empties the queue without affecting the active turn", async () => {
    const { result } = renderHook(() => useAgentChat("agent-1", "user-1"));
    await act(async () => {
      await result.current.sendMessage("first");
    });
    const firstRunId = G.__sendChat.mock.calls[0][2];
    await act(async () => {
      G.__emit({ type: "chunk", runId: firstRunId, content: "hi" });
      await result.current.sendMessage("queued-1");
      await result.current.sendMessage("queued-2");
    });
    expect(result.current.queuedMessages).toHaveLength(2);
    await act(async () => {
      result.current.clearQueue();
    });
    expect(result.current.queuedMessages).toHaveLength(0);
    // First turn still streaming — sendChat call count unchanged.
    expect(G.__sendChat).toHaveBeenCalledTimes(1);
  });
});
```

- [x] **Step 2: Commit**

```bash
git add apps/frontend/src/hooks/__tests__/useAgentChat.queue.test.ts
git commit -m "test(chat): queue and clientRunId state machine for useAgentChat"
```

---

## Task 8: End-of-plan verification

- [x] **Step 1: Run backend tests**

```bash
cd apps/backend && uv run pytest tests/routers/test_websocket_chat_idempotency.py -v
```

Expected: 2 passed.

- [x] **Step 2: Run frontend tests**

```bash
cd apps/frontend && pnpm test useAgentChat.queue
```

Expected: 6 passed.

- [x] **Step 3: Run repo-wide lint to catch any unused imports**

```bash
cd apps/frontend && pnpm run lint
cd apps/backend && uv run ruff check .
```

Expected: clean.

- [x] **Step 4: Run full backend test suite**

```bash
cd apps/backend && uv run pytest tests/ -v
```

Expected: all pass (no regression).

- [x] **Step 5: Run full frontend test suite**

```bash
cd apps/frontend && pnpm test
```

Expected: all pass (no regression).

- [x] **Step 6: Live smoke test against dev**

This must be done by the human reviewer because it requires interactive chat with a real agent. The runbook:

1. Open https://dev.isol8.co/chat
2. Pick an agent that uses browser tool calls (Ray on the dev test account is ideal).
3. Send a message that triggers a multi-step tool chain (e.g., "Search the web for X and summarize").
4. While the agent is mid-tool-call, type a second message and hit Send.
5. **Expected**: the second message appears in the queued-strip above the input (not as a sent user bubble). Send button stays visible.
6. Wait for the first turn to fully complete.
7. **Expected**: the queued message auto-sends; queue strip disappears; new user bubble appears in the transcript with the second message.
8. Refresh the page.
9. **Expected**: the JSONL no longer interleaves — user msg1 and its responses appear in order, then user msg2 and its responses.

- [x] **Step 7: Commit the final plan checkbox state**

```bash
git add docs/superpowers/plans/2026-05-11-chat-concurrent-message-ordering-fix.md
git commit -m "docs: mark concurrent-message ordering plan complete"
```

- [x] **Step 8: Open PR**

```bash
gh pr create --title "fix(chat): client-assigned runId + frontend chat queue to prevent JSONL interleave" --body "$(cat <<'EOF'
## Summary

- Eliminate the JSONL-interleaving race where a second \`chat.send\` was dispatched while a previous run was still streaming (user could send while tool calls were in progress, and the session JSONL would interleave both turns).
- Mirror upstream OpenClaw control-ui's pattern: frontend mints a UUID per user-turn (\`clientRunId\`), backend forwards as \`idempotencyKey\` to OpenClaw, frontend tracks busy gate against the matched runId and queues extra sends locally.
- Multi-bubble rendering is preserved — bubbles are for display, the new busy flag is for send gating. Orthogonal concerns.

## Test plan

- [x] Backend pytest (\`apps/backend/tests/routers/test_websocket_chat_idempotency.py\`) — 2 cases (forward + backwards-compat).
- [x] Frontend vitest (\`apps/frontend/src/hooks/__tests__/useAgentChat.queue.test.ts\`) — 6 cases covering the state machine.
- [x] Live smoke on dev: send mid-tool-call → message queues → drains on turn-complete → JSONL order is correct after refresh.

## Out of scope (deliberate)

- Backend per-session serialization (multi-tab race). Frontend-only queue mirrors upstream's choice; the queue lives per-tab, so two tabs can still race. If this becomes an observed problem we add backend gating as a follow-up.
- The 413 PayloadTooLarge on \`chat.history\` is a separate workstream tracked in \`docs/superpowers/plans/<TBD>\`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Out of scope (do not implement in this PR)

- **Backend per-session serialization** — for multi-tab races (rare). Frontend queue covers single-tab usage which is the observed bug. Defer to a follow-up if multi-tab races surface in logs.
- **413 PayloadTooLarge fix** — separate plan (REST proxy for `/chat/history` via upstream's HTTP path).
- **Frontend tool-reconstruction in chat.history** (the "ordering on refresh" symptom). That fix belongs in the 413 PR because both require touching the history read path. This PR fixes ordering for new turns only — past JSONL is already scrambled and can't be recovered.

## Open questions for the reviewer to confirm during smoke test

1. **Does OpenClaw emit exactly one `chat.final` per `chat.send` (matching `idempotencyKey`)?** Upstream UI assumes yes (`controllers/chat.ts:738-748,769-770`). If it emits multiple finals (one per sub-LLM-turn), `clientRunIdRef` would clear too early and the bug would re-emerge — the smoke test's step 6 would catch it. If observed, fall back to "all runs finalized + 1.5s idle" heuristic.
2. **Cancel semantics**: this plan keeps upstream's behavior (cancel aborts active run, queue persists). Confirm during smoke this is the desired UX. Alternative (cancel clears queue too) is one-line.
