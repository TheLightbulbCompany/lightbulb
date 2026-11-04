"use client";

// The app's own conversation with its builder, beside the preview (SessionChat's column, opened by the app's door).
import { SessionChat, type ChatWords } from "@/components/messaging/SessionChat";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { useApps, type AppRow } from "@/hooks/useApps";

const WORDS: ChatWords = {
  label: "Conversation", title: "Conversation",
  empty: "Tell the agent what to change. It answers here, and the preview updates when it's done.",
  unavailable: "This conversation isn't available to you.", full: "This conversation is full.", ended: "This conversation is closed.",
};

export function AppConversation({ app }: { app: AppRow }) {
  const { openConversation } = useApps();
  // Registered like the session chat, so the conversation on screen is not also toasted.
  const onRoom = useMessagingWorkspaceOptional()?.setSessionChatRoomId;
  return <SessionChat key={app.app_id} sessionId={app.app_id} sessionOwnerId="" openChat={openConversation} onRoom={onRoom} words={WORDS} />;
}
