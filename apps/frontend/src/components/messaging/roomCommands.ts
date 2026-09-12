"use client";

import { useMemo } from "react";
import { activateMessagesPanel } from "@/components/workspace/workspaceController";
import { useMessagingWorkspaceOptional } from "./MessagingWorkspaceContext";
import { roomDisplayName } from "@lightbulb/shared";
import { roomKindLabel } from "./types";

interface RoomCommandItem {
  id: string;
  label: string;
  sublabel: string;
  run: () => void;
}

const LIMIT = 8;
const matches = (query: string, value: string) => !query || value.toLowerCase().includes(query.toLowerCase());

export function useRoomCommandResults(query: string, enabled: boolean): RoomCommandItem[] {
  const workspace = useMessagingWorkspaceOptional();
  const directory = workspace?.directoryProps;
  const openMessage = workspace?.openMessage;
  return useMemo(() => {
    if (!enabled || !directory || !openMessage) return [];
    const rooms = directory.rooms
      .map((room) => ({ room, label: roomDisplayName(room, room.participants) }))
      .filter((entry) => matches(query, entry.label))
      .slice(0, LIMIT)
      .map(({ room, label }) => ({
        id: `room:${room.roomId}`,
        label,
        sublabel: roomKindLabel[room.kind],
        run: () => {
          openMessage(room.roomId, null);
          activateMessagesPanel();
        },
      }));
    const people = directory.participants
      .filter((participant) => matches(query, participant.displayName))
      .slice(0, LIMIT)
      .map((participant) => ({
        id: `participant:${participant.kind}:${participant.id}`,
        label: `Message ${participant.displayName}`,
        sublabel: participant.kind === "agent" ? "Agent" : "Person",
        run: () => {
          void directory.onSelectParticipant(participant);
          activateMessagesPanel();
        },
      }));
    return [...rooms, ...people];
  }, [enabled, directory, openMessage, query]);
}
