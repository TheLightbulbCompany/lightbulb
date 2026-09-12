import type { ToolCall } from "@/components/chat/ToolActivityTrace";
import type { RosterState } from "@/components/agents/AgentRoster";
import type { ReactNode } from "react";
import type {
  CanonicalRoomMessage,
  ListenMode,
  RoomMember,
  RoomSummary as CanonicalRoomSummary,
} from "@lightbulb/shared";

export type WorkState = "ready" | "queued" | "working" | "paused" | "needs-approval" | "completed" | "failed" | "offline" | "unknown";
export type RoomState = "loading" | "ready" | "reconnecting" | "unavailable" | "access-removed";

export const roomKindLabel = {
  channel: "Channel",
  dm: "Direct message",
  "group-dm": "Group chat",
} as const;

export { roomDisplayName } from "@lightbulb/shared";

export type RoomParticipant = RoomMember & { agentActivity?: RosterState };
export type DirectoryParticipant = Pick<RoomMember, "kind" | "id" | "displayName" | "mentionHandle"> & { agentActivity?: RosterState };
export type DirectoryRoom = CanonicalRoomSummary & { participants: readonly DirectoryParticipant[] };
export type RoomSummary = CanonicalRoomSummary;
export type RoomMessage = CanonicalRoomMessage & {
  contextIds?: readonly string[];
  run?: { state: WorkState; details?: string };
};

export interface PendingRoomMessage {
  clientMessageId: string;
  roomId: string;
  text: string;
  mentions: Array<{ kind: "human" | "agent"; id: string }>;
  replyToMessageId: string | null;
  threadRootMessageId: string | null;
  createdAt: string;
}

export interface RoomContext {
  id: string;
  roomId: string;
  kind: "task" | "artifact" | "approval" | "project" | "goal";
  title: string;
  status?: WorkState;
  content?: ReactNode;
  actionLabel?: string;
  onAction?: () => void;
  busy?: boolean;
}

export interface RoomRunView {
  roomId: string;
  agentId: string;
  runId: string;
  threadRootMessageId?: string | null;
  text: string;
  thinking: string;
  state: "running" | "completed" | "failed" | "aborted";
  tools: ToolCall[];
}

export interface ParticipantSelection extends Pick<RoomMember, "kind" | "id"> {
  role: "admin" | "member";
  listenMode: ListenMode | null;
}
