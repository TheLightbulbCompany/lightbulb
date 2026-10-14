import { useAuth } from "@clerk/expo";
import { useAgents } from "@lightbulb/shared";
import { Redirect, useRouter } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { findCeoDm } from "../../lib/phoneHome";
import { useMobileRooms } from "../../lib/rooms";
import { useLiveEvents } from "../../providers/live-events";

export default function PhoneHome() {
  const { userId } = useAuth();
  const router = useRouter();
  const { rooms, roomsLoading, selectAgentDm } = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const { agents, isLoading: agentsLoading } = useAgents(useLiveEvents());
  const ceoAgentId = agents.find((agent) => agent.role?.trim().toLowerCase() === "ceo")?.id;
  const ceoDm = findCeoDm(rooms, userId, ceoAgentId);
  // No workspace seeds a DM per person, so a new member has none: open (create) it once.
  const [createdRoomId, setCreatedRoomId] = useState<string | null>(null);
  const [createFailed, setCreateFailed] = useState(false);
  const asked = useRef(false);
  useEffect(() => {
    if (ceoDm || !ceoAgentId || roomsLoading || asked.current) return;
    asked.current = true;
    selectAgentDm(ceoAgentId)
      .then((roomId) => (roomId ? setCreatedRoomId(roomId) : setCreateFailed(true)))
      .catch(() => setCreateFailed(true));
  }, [ceoDm, ceoAgentId, roomsLoading, selectAgentDm]);

  const roomId = ceoDm?.roomId ?? createdRoomId;
  // Chats goes under the DM, so Back (and swipe-back) reach the tabs. A Redirect would
  // replace this screen with the room and leave nothing beneath it.
  useEffect(() => {
    if (!roomId) return;
    router.replace("/(app)/(tabs)/chats");
    router.push(`/(app)/room/${roomId}`);
  }, [roomId, router]);
  if (roomId) return null;
  const waiting = roomsLoading || agentsLoading || (!!ceoAgentId && !createFailed);
  if (waiting) return null;
  return <Redirect href="/(app)/(tabs)/chats" />;
}
