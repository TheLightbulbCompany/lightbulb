import type { ReactElement } from "react";
import type { RoomSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import type { RoomParticipant } from "./types";

const names = new Intl.ListFormat("en", { style: "long", type: "conjunction" });

/** The goal's first line, for the starter asks: short enough to read on a chip. */
const goalLine = (topic: string): string => {
  const line = topic.trim().split(/\n|(?<=[.!?])\s/)[0].replace(/[.!?]+$/, "");
  return line.length > 80 ? `${line.slice(0, 79).replace(/\s+\S*$/, "")}…` : line;
};

/** Super-agent (dev walk W12-10): 2–3 first asks built from a project's goal; a tap drafts one into the composer. */
export function starterAsks(topic: string | null | undefined): string[] {
  const goal = topic ? goalLine(topic) : "";
  return goal ? [`Make a plan for: ${goal}`, `Start on the first step of: ${goal}`, `Find out how others do it: ${goal}`] : [];
}

/**
 * Slack's "This is the very beginning of #channel": the oldest thing in the
 * timeline once history is complete, so a young room reads as a place that
 * has a beginning rather than a void above the composer. With the super-agent
 * on, a project instead opens on its goal, what to ask, and a few first asks
 * (until someone has asked something).
 */
export function RoomIntro({ room, roomName, participants, superAgent = false, fresh = false, onPickAsk }: {
  room?: RoomSummary; roomName: string; participants: readonly RoomParticipant[];
  superAgent?: boolean; fresh?: boolean; onPickAsk?: (text: string) => void;
}): ReactElement {
  const agents = participants.filter((participant) => participant.kind === "agent");
  const kind = room?.kind;
  if (superAgent && kind !== "dm" && !room?.shared && !room?.feed) {
    const asks = fresh && onPickAsk ? starterAsks(room?.topic) : [];
    return <div className="room-intro room-intro--project">
      <h3>{roomName}</h3>
      {room?.topic?.trim() && <p className="room-intro-goal">{room.topic.trim()}</p>}
      <p>Ask for anything this project needs; Lightbulb hands it to the right specialist.</p>
      {asks.length > 0 && <div className="room-intro-asks" role="group" aria-label="Suggested asks">
        {asks.map((ask) => <Button key={ask} type="button" variant="outline" size="sm" onClick={() => onPickAsk!(ask)}>
          <span className="truncate">{ask}</span></Button>)}
      </div>}
    </div>;
  }
  const title = kind === "dm" ? `This is the beginning of your direct messages with ${roomName}.`
    : `This is the beginning of ${roomName}.`;
  const detail = room?.shared ? "This project is shared with the Lightbulb team — ask us anything here."
    : kind === "dm"
    ? (agents.length > 0 ? `${roomName} replies to everything you send here.` : "Messages here are private to the two of you.")
    : agents.length === 0 ? "Mention an agent to bring one in."
      : `${names.format(agents.map((agent) => agent.displayName))} ${agents.length === 1 ? "is" : "are"} here. Mention one when you want help.`;
  return <div className="room-intro"><h3>{title}</h3><p>{detail}</p></div>;
}
