import { useAuth } from "@clerk/expo";
import { useAgents } from "@lightbulb/shared";
import { Redirect } from "expo-router";
import { findCeoDm } from "../../lib/phoneHome";
import { useMobileRooms } from "../../lib/rooms";
import { useLiveEvents } from "../../providers/live-events";

export default function PhoneHome() {
  const { userId } = useAuth();
  const { rooms, roomsLoading } = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const { agents, isLoading: agentsLoading } = useAgents(useLiveEvents());
  const ceoAgentId = agents.find((agent) => agent.role?.trim().toLowerCase() === "ceo")?.id;
  const ceoDm = findCeoDm(rooms, userId, ceoAgentId);
  if (ceoDm) return <Redirect href={`/(app)/room/${ceoDm.roomId}`} />;
  if (roomsLoading || agentsLoading) return null;
  return <Redirect href="/(app)/(tabs)/chats" />;
}