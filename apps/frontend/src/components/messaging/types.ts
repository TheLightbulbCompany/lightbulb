import type { ReactNode } from "react";

/** Presentation records only. The caller supplies authorized, verified identities. */
export interface RoomParticipant {
  ref: string;
  name: string;
  kind: "human" | "agent";
  agentState?: WorkState;
}

export type WorkState = "ready" | "queued" | "working" | "paused" | "needs-approval" | "completed" | "failed" | "offline" | "unknown";
export type RoomState = "loading" | "ready" | "reconnecting" | "unavailable" | "access-removed";
export type RoomKind = "public" | "private" | "dm" | "group";

export const roomKindLabel: Record<RoomKind, string> = {
  public: "Public room", private: "Private room", dm: "Direct message", group: "Group message",
};

export interface RoomSummary {
  id: string;
  name: string;
  kind: RoomKind;
  unread?: boolean;
  mentionCount?: number;
  approvalCount?: number;
}

export interface RoomMessage {
  id: string;
  roomId: string;
  /** Historical snapshot supplied by the authorized adapter, never current membership.
   * This local shape does not verify native sender evidence; absent evidence stays unknown. */
  readonly author?: Readonly<Pick<RoomParticipant, "ref" | "name" | "kind">>;
  body: string;
  timestamp?: string;
  contextIds?: readonly string[];
  run?: { state: WorkState; details?: string };
}

/** Existing authorized work, never an inferred record parsed from message text. */
export interface RoomContext {
  id: string;
  roomId: string;
  kind: "task" | "artifact" | "approval";
  title: string;
  status?: WorkState;
  content?: ReactNode;
  actionLabel?: string;
  onAction?: () => void;
  busy?: boolean;
}
