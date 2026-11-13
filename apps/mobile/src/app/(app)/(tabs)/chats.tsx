import { resolveCeoAgentId } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { ScrollView, StyleSheet } from "react-native";

import { TrialRecapCard } from "../../../components/billing/TrialRecapCard";
import { FocusedStatusBar } from "../../../components/ui/FocusedStatusBar";
import { VoiceHome } from "../../../components/voice/VoiceHome";
import { useChatPalette } from "../../../config/theme";
import { useMobileRooms } from "../../../lib/rooms";
import { useWorkspaceRoster } from "../../../lib/workspaceRoster";

/** Home: the orb, New chat, and recent calls, meetings and text chats with the assistant. */
export default function Home() {
  const palette = useChatPalette();
  const { rooms, createRoom } = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const { agents } = useWorkspaceRoster(true);
  const agentId = resolveCeoAgentId(agents);
  // A new chat is a group chat seating only the assistant: a DM with it is unique per person (the orb's room),
  // and the server allows a one-agent group chat. `activate: false`: this list instance never shows the room.
  const newChat = async () => (await createRoom({
    clientRoomId: randomUUID(), kind: "group-dm", name: null,
    members: [{ kind: "agent", id: agentId!, role: "member", listenMode: "active" }],
  }, { activate: false })).roomId;
  return (
    <ScrollView testID="voice-home" contentInsetAdjustmentBehavior="automatic"
      style={[styles.page, { backgroundColor: palette.surface }]}>
      <FocusedStatusBar />
      <VoiceHome rooms={rooms} agentId={agentId} onNewChat={agentId ? newChat : undefined} />
      <TrialRecapCard />
    </ScrollView>
  );
}

const styles = StyleSheet.create({ page: { flex: 1 } });
