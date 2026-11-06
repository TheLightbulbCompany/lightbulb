import type { RosterState } from "@/components/agents/rosterState";
import type {
  CanonicalRoomMessage,
  ListenMode,
  RoomMember,
  RoomSummary,
} from "@lightbulb/shared";

export type WorkState = "ready" | "queued" | "working" | "paused" | "stopped" | "needs-approval" | "completed" | "failed" | "offline" | "unknown";
export type RoomState = "loading" | "ready" | "reconnecting" | "unavailable" | "access-removed";

export const roomKindLabel = {
  channel: "Channel",
  dm: "Direct message",
  "group-dm": "Group chat",
} as const;

/** `online` is presence for people (a recent heartbeat from a tab in use); agents carry `agentActivity` instead.
 *  `sharing` is a live shared terminal session, from the workspace list. */
export type RoomParticipant = RoomMember & { agentActivity?: RosterState; online?: boolean; sharing?: boolean };
export type DirectoryParticipant = Pick<RoomMember, "kind" | "id" | "displayName" | "mentionHandle"> & { agentActivity?: RosterState; online?: boolean; sharing?: boolean };
export type DirectoryRoom = RoomSummary & { participants: readonly DirectoryParticipant[] };
export type RoomMessage = CanonicalRoomMessage;

export interface PendingRoomMessage {
  clientMessageId: string;
  roomId: string;
  text: string;
  mentions: Array<{ kind: "human" | "agent"; id: string }>;
  replyToMessageId: string | null;
  threadRootMessageId: string | null;
  createdAt: string;
}

export interface ParticipantSelection extends Pick<RoomMember, "kind" | "id"> {
  role: "admin" | "member";
  listenMode: ListenMode | null;
}
