"use client";

// The app's own conversation with its builder, beside the preview (SessionChat's column, opened by the app's door).
import { useEffect, useState } from "react";
import { SessionChat, type ChatWords } from "@/components/messaging/SessionChat";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { useApps, type AppRow } from "@/hooks/useApps";

const WORDS: ChatWords = {
  label: "Conversation", title: "Conversation",
  empty: "Tell the agent what to change. It answers here, and the preview updates when it's done.",
  unavailable: "This conversation isn't available to you.", full: "This conversation is full.", ended: "This conversation is closed.",
};

/** `shown: false`: mounted but hidden (a narrow panel showing the app), so its messages still notify. */
export function AppConversation({ app, shown = true }: { app: AppRow; shown?: boolean }) {
  const { openConversation } = useApps();
  const setSideRoom = useMessagingWorkspaceOptional()?.setSideRoom;
  const [roomId, setRoomId] = useState<string | null>(null);
  // Registered under its own key, like the session chat, so the conversation on screen is not also toasted.
  const key = `app-conversation:${app.app_id}`;
  useEffect(() => {
    if (!setSideRoom) return;
    setSideRoom(key, shown ? roomId : null);
    return () => setSideRoom(key, null);
  }, [setSideRoom, key, shown, roomId]);
  return <SessionChat key={app.app_id} sessionId={app.app_id} sessionOwnerId="" openChat={openConversation} onRoom={setRoomId} words={WORDS} />;
}
