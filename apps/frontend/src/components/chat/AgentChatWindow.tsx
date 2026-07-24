"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";

import { capture } from "@/lib/analytics";
import { ChatInput } from "./ChatInput";
import { ModelEffortPill } from "./ModelEffortPill";
import { useBilling } from "@/hooks/useBilling";
import { ExecApprovalCards } from "./ExecApprovalCards";
import { MessageList, MessageListHandle } from "./MessageList";
import { StreamingStatusBar } from "./StreamingStatusBar";
import { useAgentChat, BOOTSTRAP_MESSAGE } from "@/hooks/useAgentChat";
import { useAgentSessions } from "@/hooks/useAgentSessions";
import { resolveSessionModelRef } from "@/lib/models";
import { useAgents, agentDisplayName } from "@/hooks/useAgents";
import { useApi } from "@/lib/api";
import { isSnoozed, setSnoozed, updateSnoozeKey } from "@/lib/snooze";
import { useAuth, useOrganization } from "@clerk/nextjs";
import { Loader2, RefreshCw, Clock, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useGateway } from "@/hooks/useGateway";

import type { ChatIncomingMessage } from "@/hooks/useGateway";

interface AgentChatWindowProps {
  agentId: string | null;
  onOpenFile?: (path: string) => void;
}

// Owner-scoped once-ever marker so `trial_first_agent_response` fires at most
// once per trial owner — same idiom as ProvisioningStepper's
// ONBOARDING_READY_STORAGE_KEY / TRIAL_STARTED_STORAGE_KEY.
const TRIAL_FIRST_RESPONSE_STORAGE_KEY = "isol8_trial_first_response_v1";

// =============================================================================
// Update Banner
// =============================================================================

interface PendingUpdate {
  update_id: string;
  description: string;
  type: string;
  status: string;
  created_at: string;
  scheduled_at?: string;
}


function UpdateBanner() {
  const api = useApi();
  const { organization, membership } = useOrganization();
  const { onChatMessage } = useGateway();

  const [applying, setApplying] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  // Updates the user acted on locally (applied / scheduled / snoozed) —
  // hidden immediately without waiting for a refetch.
  const [removedIds, setRemovedIds] = useState<ReadonlySet<string>>(new Set());

  const isOrg = !!organization;
  const isOrgAdmin = membership?.role === "org:admin";

  // SWR (vs. a per-instance fetch effect) so the banner's render branches
  // and AgentChatWindow's key-driven remounts dedupe onto one request per
  // window instead of refetching on every mount.
  const { data, mutate: refetchUpdates } = useSWR<{ updates: PendingUpdate[] }>(
    "/container/updates",
    async (url: string) => (await api.get(url)) as { updates: PendingUpdate[] },
    // Errors are left to throw (endpoint may not exist yet) so SWR keeps
    // the PREVIOUS data — swallowing them into {updates: []} would make a
    // transient network error hide an already-visible banner. No retry:
    // update_available events drive refetches.
    { revalidateOnFocus: false, dedupingInterval: 15_000, shouldRetryOnError: false },
  );

  const updates = useMemo(
    () =>
      (data?.updates || []).filter(
        (u) =>
          u.status === "pending" &&
          !isSnoozed(updateSnoozeKey(u.update_id)) &&
          !removedIds.has(u.update_id),
      ),
    [data, removedIds],
  );

  const hideUpdate = useCallback((updateId: string) => {
    setRemovedIds((prev) => new Set(prev).add(updateId));
  }, []);

  // Listen for update_available WS events to trigger refetch
  useEffect(() => {
    const unsub = onChatMessage((msg: ChatIncomingMessage) => {
      if (msg.type === "update_available") {
        setDismissed(false);
        refetchUpdates();
      }
    });
    return unsub;
  }, [onChatMessage, refetchUpdates]);

  const handleApplyNow = useCallback(
    async (updateId: string) => {
      setApplying(true);
      try {
        await api.post(`/container/updates/${updateId}/apply`, { schedule: "now" });
        capture("update_applied");
        hideUpdate(updateId);
      } catch (err) {
        console.error("Failed to apply update:", err);
      } finally {
        setApplying(false);
      }
    },
    [api, hideUpdate],
  );

  const handleScheduleTonight = useCallback(
    async (updateId: string) => {
      try {
        await api.post(`/container/updates/${updateId}/apply`, { schedule: "tonight" });
        capture("update_scheduled");
        hideUpdate(updateId);
      } catch (err) {
        console.error("Failed to schedule update:", err);
      }
    },
    [api, hideUpdate],
  );

  const handleRemindLater = useCallback(
    async (updateId: string) => {
      try {
        await api.post(`/container/updates/${updateId}/apply`, { schedule: "remind_later" });
      } catch {
        // best-effort
      }
      // Snooze for 4 hours
      setSnoozed(updateSnoozeKey(updateId), 4 * 60 * 60 * 1000);
      hideUpdate(updateId);
    },
    [api, hideUpdate],
  );

  if (dismissed || updates.length === 0) return null;

  const firstUpdate = updates[0];
  const description =
    updates.length > 1
      ? `${updates.length} updates available`
      : firstUpdate.description;

  // During apply -- show spinner state
  if (applying) {
    return (
      <div className="mx-4 mb-2 p-3 bg-[#e8f5e9] border border-[#c8e6c9] rounded-lg flex items-center gap-3">
        <Loader2 className="h-4 w-4 text-[#2d8a4e] shrink-0 animate-spin" />
        <p className="text-sm text-[#1a5c32] flex-1">Updating your agent...</p>
      </div>
    );
  }

  // Org member (non-admin) -- info only
  if (isOrg && !isOrgAdmin) {
    return (
      <div className="mx-4 mb-2 p-3 bg-[#e8f5e9] border border-[#c8e6c9] rounded-lg flex items-center gap-3">
        <RefreshCw className="h-4 w-4 text-[#2d8a4e] shrink-0" />
        <p className="text-sm text-[#1a5c32] flex-1">
          An update is available. Your admin can apply it.
        </p>
        <button
          onClick={() => setDismissed(true)}
          className="text-[#2d8a4e] hover:text-[#2d8a4e] shrink-0"
          aria-label="Dismiss"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    );
  }

  return (
    <div className="mx-4 mb-2 p-3 bg-[#e8f5e9] border border-[#c8e6c9] rounded-lg">
      <div className="flex items-center gap-3">
        <RefreshCw className="h-4 w-4 text-[#2d8a4e] shrink-0" />
        <div className="flex-1 min-w-0">
          <p className="text-sm text-[#1a5c32]">
            Update available: {description}
          </p>
          <p className="text-xs text-[#2d7a50] mt-0.5">
            Your agent needs a brief restart (~30s) to apply.
          </p>
        </div>
        <button
          onClick={() => setDismissed(true)}
          className="text-[#2d8a4e] hover:text-[#2d8a4e] shrink-0"
          aria-label="Dismiss"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="flex items-center gap-2 mt-2 ml-7 flex-wrap">
        <Button
          size="sm"
          variant="outline"
          className="shrink-0 border-[#c8e6c9] text-[#1a5c32] hover:bg-[#c8e6c9]"
          onClick={() => handleApplyNow(firstUpdate.update_id)}
        >
          <RefreshCw className="h-3 w-3 mr-1" />
          Update Now
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="shrink-0 border-[#c8e6c9] text-[#1a5c32] hover:bg-[#c8e6c9]"
          onClick={() => handleScheduleTonight(firstUpdate.update_id)}
        >
          <Clock className="h-3 w-3 mr-1" />
          Tonight at 2 AM
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="shrink-0 border-[#c8e6c9] text-[#1a5c32] hover:bg-[#c8e6c9]"
          onClick={() => handleRemindLater(firstUpdate.update_id)}
        >
          Remind Me Later
        </Button>
      </div>
    </div>
  );
}

export function AgentChatWindow({
  agentId,
  onOpenFile,
}: AgentChatWindowProps): React.ReactElement {
  const { userId } = useAuth();
  const { organization } = useOrganization();
  // Every user gets their own session — isolates chat history from cron,
  // channels, and other system activity. Matches backend (websocket_chat.py).
  // userId is always defined here — ChatLayout gates on clerkLoaded && isSignedIn.
  const sessionName = userId!;
  const { account } = useBilling();
  // Free-tier discriminator — same field ProvisioningStepper uses to derive
  // isTrialLocal. useAgentChat has no billing context, so this component
  // (which already reads useBilling) decides whether a completed turn
  // counts as the `trial_first_agent_response` milestone.
  const isTrialLocal = account?.subscription_status === "trial_local";
  const trialFirstResponseFiredRef = useRef(false);
  const handleChatCompleted = useCallback(() => {
    if (!isTrialLocal || trialFirstResponseFiredRef.current) return;
    trialFirstResponseFiredRef.current = true;
    const storageKey = `${TRIAL_FIRST_RESPONSE_STORAGE_KEY}:${organization?.id ?? userId ?? "anon"}`;
    let alreadyFired = false;
    try {
      alreadyFired = window.localStorage.getItem(storageKey) === "1";
    } catch {
      // storage-disabled browser: best-effort only, may re-fire on reload
    }
    if (alreadyFired) return;
    try {
      window.localStorage.setItem(storageKey, "1");
    } catch {
      /* best-effort */
    }
    capture("trial_first_agent_response");
  }, [isTrialLocal, organization?.id, userId]);
  const {
    renderGroups,
    renderItems,
    isStreaming,
    busy,
    sendMessage,
    cancelMessage,
    clearMessages,
    isConnected,
    isLoadingHistory,
    needsBootstrap,
    queuedMessages,
    removeQueuedMessage,
    clearQueue,
    steerMessage,
    retryQueuedMessage,
    activeSessionId,
    historyTruncated,
    hasOlderMessages,
    isLoadingOlder,
    loadOlderMessages,
  } = useAgentChat(agentId, sessionName, handleChatCompleted);

  // Hydrate the composer pill from the ACTIVE session's persisted row so it
  // reflects what sessions.patch wrote, not a stub. useAgentSessions shares its
  // SWR cache with the sidebar (same key), so this is not a second fetch; the
  // active row is matched on sessionId (null == the legacy "General" row).
  const { sessions: agentSessions, mutate: mutateSessions } = useAgentSessions(
    agentId,
    userId ?? null,
  );
  const activeSessionRow = agentSessions.find((s) => s.sessionId === activeSessionId);

  const modelEffortPill =
    agentId != null ? (
      <ModelEffortPill
        agentId={agentId}
        activeSessionId={activeSessionId}
        isStreaming={isStreaming}
        selectedModelRef={resolveSessionModelRef(
          activeSessionRow?.model,
          activeSessionRow?.modelProvider,
        )}
        thinkingLevel={activeSessionRow?.thinkingLevel ?? ""}
        fastMode={activeSessionRow?.fastMode ?? false}
        providerChoice={account?.provider_choice ?? null}
        byoProvider={account?.byo_provider ?? null}
        onChanged={() => {
          // A patch persisted — revalidate sessions.list so the pill (and
          // sidebar) reflect the new model/effort on the next render.
          void mutateSessions();
        }}
      />
    ) : null;

  const { agents } = useAgents();
  const activeAgent = agents.find((a) => a.id === agentId);
  const agentName = activeAgent?.identity?.name ?? activeAgent?.name ?? agentId ?? undefined;

  const api = useApi();
  const [isUploading, setIsUploading] = useState(false);
  const messageListRef = useRef<MessageListHandle>(null);

  const isInitialState = renderGroups.length === 0;

  // sessions.list only changes server-side on run activity (materialization,
  // derived titles, token counts) — SWR has no push channel for it, so
  // revalidate on the run-completion edge to keep the sidebar rows current.
  const prevStreamingRef = useRef(false);
  useEffect(() => {
    if (prevStreamingRef.current && !isStreaming) void mutateSessions();
    prevStreamingRef.current = isStreaming;
  }, [isStreaming, mutateSessions]);

  const prevAgentIdRef = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    if (
      prevAgentIdRef.current !== undefined &&
      prevAgentIdRef.current !== agentId
    ) {
      clearMessages();
    }
    prevAgentIdRef.current = agentId;
  }, [agentId, clearMessages]);

  const handleSend = useCallback(
    async (content: string, files?: File[]): Promise<void> => {
      try {
        let message = content;

        if (files && files.length > 0) {
          setIsUploading(true);
          try {
            if (!agentId) throw new Error("No agent selected");
            const result = await api.uploadFiles(files, agentId);
            const fileList = result.uploaded
              .map((f) => `- ${f.filename} → ${f.path}`)
              .join("\n");
            const fileNotice = `[The user uploaded files to your workspace. You can read them at these paths:\n${fileList}]\n\n`;
            message = fileNotice + message;
          } catch (err) {
            console.error("Upload failed:", err);
            const errorMsg = err instanceof Error ? err.message : "Upload failed";
            // Still send the text message but note the upload failure
            message = `[File upload failed: ${errorMsg}]\n\n` + message;
          } finally {
            setIsUploading(false);
          }
        }

        if (message.trim()) {
          await sendMessage(message);
          // A first message materializes the session's sessions.list row —
          // revalidate so the sidebar shows the new session immediately.
          void mutateSessions();
          setTimeout(() => messageListRef.current?.scrollToBottom(), 50);
        }
      } catch (err) {
        console.error("Failed to send message:", err);
      }
    },
    [sendMessage, api, agentId],
  );

  // NOTE: there is deliberately no `if (chatError)` branch. Errors render as
  // ChatErrorCard bubbles inside the transcript (useAgentChat appends one per
  // failed run), which keeps them in run order and lets them scroll away. An
  // error must never swap the panel's layout: this branch used to hand-roll its
  // own flex column without `min-h-0`, so the message list grew to full
  // transcript height and pushed ChatInput past the dock panel's clip boundary
  // — unreachable, with no way to scroll to it. Mobile (ChatLayout) has always
  // rendered errors transcript-only; this is the same model.

  if (isLoadingHistory) {
    // Render the chat SHELL while chat.history is in flight so the chat
    // box appears the moment the container is ready, instead of a bare
    // full-pane spinner. The input is disabled for this brief window
    // only: a send dispatched before the history projection lands would
    // be clobbered when setChatMessages replaces the message list.
    return (
      <div className="flex flex-col h-full min-h-0 bg-[#faf7f2]">
        {/* Mirror MessageList's geometry (same pane padding, max-w-3xl column,
            space-y-10 turn rhythm) and anchor to the bottom, where a loaded
            transcript sits — so the placeholder occupies the region the
            history will actually replace, shaped like conversation turns. */}
        <div
          className="flex-1 min-h-0 overflow-hidden flex flex-col justify-end p-4 md:px-8"
          aria-busy="true"
        >
          <div className="w-full max-w-3xl mx-auto space-y-10 py-8">
            <div className="space-y-2.5">
              <div className="h-3.5 w-11/12 rounded bg-[#eee9df] animate-pulse motion-reduce:animate-none" />
              <div className="h-3.5 w-3/5 rounded bg-[#eee9df] animate-pulse motion-reduce:animate-none [animation-delay:150ms]" />
            </div>
            {/* Echoes the real user bubble: right-aligned, rounded-2xl/br-md. */}
            <div className="flex justify-end">
              <div className="h-9 w-2/5 rounded-2xl rounded-br-md bg-[#f0ebe2] animate-pulse motion-reduce:animate-none [animation-delay:300ms]" />
            </div>
            <div className="space-y-2.5">
              <div className="h-3.5 w-4/5 rounded bg-[#eee9df] animate-pulse motion-reduce:animate-none [animation-delay:450ms]" />
              <div className="h-3.5 w-full rounded bg-[#eee9df] animate-pulse motion-reduce:animate-none [animation-delay:600ms]" />
              <div className="h-3.5 w-2/5 rounded bg-[#eee9df] animate-pulse motion-reduce:animate-none [animation-delay:750ms]" />
            </div>
          </div>
        </div>
        <ChatInput
          onSend={handleSend}
          onStop={cancelMessage}
          agentId={agentId}
          disabled
          isStreaming={isStreaming}
          isConnected={isConnected}
          isUploading={isUploading}
          queuedMessages={queuedMessages}
          onRemoveQueued={removeQueuedMessage}
          onClearQueue={clearQueue}
          onSteerQueued={steerMessage}
          onRetryQueued={retryQueuedMessage}
          controls={modelEffortPill}
        />
      </div>
    );
  }

  if (isInitialState) {
    return (
      <div className="flex flex-col h-full min-h-0 bg-[#faf7f2]">
        {/* justify-center-safe, not justify-center: unsafe centering splits the
            overflow across BOTH ends, so once the content outgrows a short dock
            slot the top is clipped and unreachable even with overflow-y-auto
            (measured: scrollHeight 300 for 400px of content, top pinned at
            -100px). Safe centering falls back to flex-start when it overflows —
            same "present but unreachable" failure this branch exists to fix,
            reached by a different mechanism. */}
        <div className="flex-1 min-h-0 overflow-y-auto flex flex-col items-center justify-center-safe p-4">
          <div className="text-center mb-8">
            <div className="mb-6 flex justify-center">
              <span style={{ fontFamily: "var(--font-lora), serif", fontStyle: "italic", fontWeight: 400, fontSize: "56px", color: "#1a1a1a", lineHeight: 1 }}>8</span>
            </div>
            <h1 className="text-3xl mb-3 text-[#1a1a1a] tracking-tight font-lora">
              {agentId
                ? agentDisplayName(activeAgent ?? { id: agentId })
                : "Select an agent"}
            </h1>
            <p className="text-[#8a8578] text-base">
              Start a conversation with your agent
            </p>
          </div>
          <div className="w-full max-w-2xl">
            <UpdateBanner />
            <ChatInput
              onSend={handleSend}
              onStop={cancelMessage}
              agentId={agentId}
              isConnected={isConnected}
              disabled={!agentId}
              isStreaming={isStreaming}
              centered
              isUploading={isUploading}
              suggestedMessage={needsBootstrap ? BOOTSTRAP_MESSAGE : undefined}
              queuedMessages={queuedMessages}
              onRemoveQueued={removeQueuedMessage}
              onClearQueue={clearQueue}
              onSteerQueued={steerMessage}
              onRetryQueued={retryQueuedMessage}
              controls={modelEffortPill}
            />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-h-0 bg-[#faf7f2]">
      {historyTruncated && (
        <div className="shrink-0 px-4 py-1.5 text-center text-[11px] text-[#8a8578] bg-[#f3efe6] border-b border-[#e0dbd0]">
          Some large tool output in this conversation was shortened to fit.
        </div>
      )}
      <MessageList
        ref={messageListRef}
        renderGroups={renderGroups}
        agentName={agentName}
        onOpenFile={onOpenFile}
        isStreaming={isStreaming}
        isTyping={isStreaming}
        hasOlder={hasOlderMessages}
        isLoadingOlder={isLoadingOlder}
        onLoadOlder={loadOlderMessages}
      />
      <UpdateBanner />
      <StreamingStatusBar isStreaming={isStreaming} renderItems={renderItems} active={busy} />
      <ExecApprovalCards />
      <ChatInput
        onSend={handleSend}
        onStop={cancelMessage}
        agentId={agentId}
        isConnected={isConnected}
        disabled={!agentId}
        isStreaming={isStreaming}
        isUploading={isUploading}
        queuedMessages={queuedMessages}
        onRemoveQueued={removeQueuedMessage}
        onClearQueue={clearQueue}
        onSteerQueued={steerMessage}
        onRetryQueued={retryQueuedMessage}
        controls={modelEffortPill}
      />
    </div>
  );
}
