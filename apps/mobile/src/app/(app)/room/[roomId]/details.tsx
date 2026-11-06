import { useAuth } from "@clerk/expo";
import { agentDisplayName, useAgents, type MemberRef } from "@lightbulb/shared";
import { router, useLocalSearchParams } from "expo-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { View } from "react-native";

import { RoomBlockedState } from "../../../../components/room/RoomBlockedState";
import { RoomDetails } from "../../../../components/room/RoomDetails";
import { useChatPalette } from "../../../../config/theme";
import { useMobileRooms } from "../../../../lib/rooms";
import { fetchMembers, useTeamsConfig } from "../../../../lib/teamsData";
import { useLiveEvents } from "../../../../providers/live-events";

/**
 * The room header's details screen. Its own room machine selecting the same
 * room, as the thread screen does (the SWR keys are shared, so no extra
 * fetch). `visible: false`: reading details marks nothing read.
 */
export default function RoomDetailsScreen() {
  const palette = useChatPalette();
  const { roomId, addPeople } = useLocalSearchParams<{ roomId: string; addPeople?: string }>();
  const { userId } = useAuth();
  const rooms = useMobileRooms({ subscribeToRoom: false, autoSelect: false, visible: false });

  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);

  // Add members' candidates: the owner's agents plus the workspace's people.
  const { agents } = useAgents(useLiveEvents());
  const config = useTeamsConfig();
  const configRef = useRef(config);
  configRef.current = config;
  const [people, setPeople] = useState<(MemberRef & { displayName: string })[]>([]);
  useEffect(() => {
    let alive = true;
    void fetchMembers(configRef.current).then((members) => {
      if (alive) {
        setPeople(members.map((m) => ({ kind: "human" as const, id: m.userId, displayName: m.name || m.email || "Workspace member" })));
      }
    });
    return () => {
      alive = false;
    };
  }, []);
  const candidates = useMemo(
    () => [...agents.map((a) => ({ kind: "agent" as const, id: a.id, displayName: agentDisplayName(a) })), ...people],
    [agents, people],
  );

  const room = rooms.activeRoom?.roomId === roomId ? rooms.activeRoom : null;
  if (!room) {
    const state = rooms.accessRemoved ? "access-removed" : rooms.roomError || rooms.selectRoomError ? "unavailable" : "loading";
    return (
      <View style={{ flex: 1, backgroundColor: palette.ground }}>
        <RoomBlockedState state={state} onRetry={state === "unavailable" ? rooms.refreshRoom : undefined} />
      </View>
    );
  }
  return (
    <RoomDetails
      room={room}
      currentUserId={userId ?? ""}
      candidates={candidates}
      // Adding someone to a DM starts a new group chat (the DM stays as it was): go there.
      upsertMember={async (request) => {
        const updated = await rooms.upsertMember(request);
        if (updated.roomId !== room.roomId) router.replace(`/(app)/room/${updated.roomId}`);
      }}
      removeMember={rooms.removeMember}
      initialAddPeopleOpen={addPeople === "1"}
      // Past the conversation too: it is gone for this reader.
      leaveRoom={async () => {
        await rooms.leaveRoom(room.roomId);
        router.dismiss(2);
      }}
      setArchived={(archived) => rooms.updateRoom({ archived })}
      renameRoom={async (name) => {
        await rooms.updateRoom({ name });
      }}
    />
  );
}
