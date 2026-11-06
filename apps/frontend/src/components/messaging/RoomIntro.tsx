import type { ReactElement } from "react";
import type { RoomSummary } from "@lightbulb/shared";
import type { RoomParticipant } from "./types";

const names = new Intl.ListFormat("en", { style: "long", type: "conjunction" });

/**
 * Slack's "This is the very beginning of #channel": the oldest thing in the
 * timeline once history is complete, so a young room reads as a place that
 * has a beginning rather than a void above the composer.
 */
export function RoomIntro({ room, roomName, participants }: { room?: RoomSummary; roomName: string; participants: readonly RoomParticipant[] }): ReactElement {
  const agents = participants.filter((participant) => participant.kind === "agent");
  const kind = room?.kind;
  const title = kind === "dm" ? `This is the beginning of your direct messages with ${roomName}.`
    : `This is the beginning of ${roomName}.`;
  const detail = room?.shared ? "This project is shared with the Lightbulb team — ask us anything here."
    : kind === "dm"
    ? (agents.length > 0 ? `${roomName} replies to everything you send here.` : "Messages here are private to the two of you.")
    : agents.length === 0 ? "Mention an agent to bring one in."
      : `${names.format(agents.map((agent) => agent.displayName))} ${agents.length === 1 ? "is" : "are"} here. Mention one when you want help.`;
  return <div className="room-intro"><h3>{title}</h3><p>{detail}</p></div>;
}
