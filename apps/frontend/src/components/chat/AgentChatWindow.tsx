"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { capture } from "@/lib/analytics";
import { ChatInput } from "./ChatInput";
import { ExecApprovalCards } from "./ExecApprovalCards";
import { MessageList, MessageListHandle } from "./MessageList";
import { StreamingStatusBar } from "./StreamingStatusBar";
import { useAgentChat, BOOTSTRAP_MESSAGE } from "@/hooks/useAgentChat";
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

  const [updates, setUpdates] = useState<PendingUpdate[]>([]);
  const [applying, setApplying] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  const isOrg = !!organization;
  const isOrgAdmin = membership?.role === "org:admin";

  const fetchUpdates = useCallback(async () => {
    try {
      const data = (await api.get("/container/updates")) as { updates: PendingUpdate[] };
      const pending = (data.updates || []).filter(
        (u) => u.status === "pending" && !isSnoozed(updateSnoozeKey(u.update_id)),
      );
      setUpdates(pending);
      setDismissed(false);
    } catch {
      // Endpoint may not exist yet -- silently ignore
    }
  }, [api]);

  // Fetch on mount
  useEffect(() => {
    fetchUpdates();
  }, [fetchUpdates]);

  // Listen for update_available WS events to trigger refetch
  useEffect(() => {
    const unsub = onChatMessage((msg: ChatIncomingMessage) => {
      if (msg.type === "update_available") {
        fetchUpdates();
      }
    });
    return unsub;
  }, [onChatMessage, fetchUpdates]);

  const handleApplyNow = useCallback(
    async (updateId: string) => {
      setApplying(true);
      try {
        await api.post(`/container/updates/${updateId}/apply`, { schedule: "now" });
        capture("update_applied");
        setUpdates((prev) => prev.filter((u) => u.update_id !== updateId));
      } catch (err) {
        console.error("Failed to apply update:", err);
      } finally {
        setApplying(false);
      }
    },
    [api],
  );

  const handleScheduleTonight = useCallback(
    async (updateId: string) => {
      try {
        await api.post(`/container/updates/${updateId}/apply`, { schedule: "tonight" });
        capture("update_scheduled");
        setUpdates((prev) => prev.filter((u) => u.update_id !== updateId));
      } catch (err) {
        console.error("Failed to schedule update:", err);
      }
    },
    [api],
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
      setUpdates((prev) => prev.filter((u) => u.update_id !== updateId));
    },
    [api],
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
  // Every user gets their own session — isolates chat history from cron,
  // channels, and other system activity. Matches backend (websocket_chat.py).
  // userId is always defined here — ChatLayout gates on clerkLoaded && isSignedIn.
  const sessionName = userId!;
  const {
    renderGroups,
    renderItems,
    isStreaming,
    error: chatError,
    sendMessage,
    cancelMessage,
    clearMessages,
    isLoadingHistory,
    needsBootstrap,
    queuedMessages,
    removeQueuedMessage,
    clearQueue,
  } = useAgentChat(agentId, sessionName);

  const { agents } = useAgents();
  const activeAgent = agents.find((a) => a.id === agentId);
  const agentName = activeAgent?.identity?.name ?? activeAgent?.name ?? agentId ?? undefined;

  const api = useApi();
  const [isUploading, setIsUploading] = useState(false);
  const messageListRef = useRef<MessageListHandle>(null);

  const isInitialState = renderGroups.length === 0;

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
          setTimeout(() => messageListRef.current?.scrollToBottom(), 50);
        }
      } catch (err) {
        console.error("Failed to send message:", err);
      }
    },
    [sendMessage, api, agentId],
  );

  if (chatError) {
    return (
      <div className="flex flex-col h-full bg-[#faf7f2]">
        <div className="flex-1 flex flex-col">
          {renderGroups.length > 0 && (
            <MessageList
              ref={messageListRef}
              renderGroups={renderGroups}
              agentName={agentName}
              onOpenFile={onOpenFile}
              isStreaming={isStreaming}
              isTyping={isStreaming}
            />
          )}
          <div className="p-4 m-4 bg-[#fce4ec] border border-[#f8bbd0] text-[#a5311f] rounded-lg">
            <p className="font-medium">Error</p>
            <p className="text-sm">{chatError}</p>
          </div>
          <StreamingStatusBar isStreaming={isStreaming} renderItems={renderItems} />
          <ExecApprovalCards />
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
        </div>
      </div>
    );
  }

  if (isLoadingHistory) {
    return (
      <div className="flex flex-col h-full bg-[#faf7f2]">
        <div className="flex-1 flex items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-[#8a8578]" />
        </div>
      </div>
    );
  }

  if (isInitialState) {
    return (
      <div className="flex flex-col h-full bg-[#faf7f2]">
        <div className="flex-1 flex flex-col items-center justify-center p-4">
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
              disabled={!agentId}
              isStreaming={isStreaming}
              centered
              isUploading={isUploading}
              suggestedMessage={needsBootstrap ? BOOTSTRAP_MESSAGE : undefined}
              queuedMessages={queuedMessages}
              onRemoveQueued={removeQueuedMessage}
              onClearQueue={clearQueue}
            />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-h-0 bg-[#faf7f2]">
      <MessageList
        ref={messageListRef}
        renderGroups={renderGroups}
        agentName={agentName}
        onOpenFile={onOpenFile}
        isStreaming={isStreaming}
        isTyping={isStreaming}
      />
      <UpdateBanner />
      <StreamingStatusBar isStreaming={isStreaming} renderItems={renderItems} />
      <ExecApprovalCards />
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
    </div>
  );
}
