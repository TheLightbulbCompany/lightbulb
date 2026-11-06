import type { ReactElement } from "react";
import type { RoomSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import type { RoomParticipant } from "./types";

const names = new Intl.ListFormat("en", { style: "long", type: "conjunction" });

/** The goal's first line, for asks built from it: one sentence, without its end mark. */
export const goalLine = (topic: string | null | undefined): string => {
  const line = (topic ?? "").trim().split(/\n|(?<=[.!?])\s/)[0].replace(/[.!?]+$/, "").trim();
  return line.length > 160 ? `${line.slice(0, 159).replace(/\s+\S*$/, "")}…` : line;
};

export interface StarterAsk { label: string; ask: string }

/** Super-agent (W12-10, W13-6): three short, distinct chips; a tap drafts a full ask built from the project's goal into
 *  the composer, or a general one when the project has no goal. */
export function starterAsks(topic: string | null | undefined): StarterAsk[] {
  const goal = goalLine(topic);
  if (!goal) return [
    { label: "Plan it", ask: "Make a short step-by-step plan for this channel. Ask me what you need to know first." },
    { label: "First draft", ask: "Write a first draft of what this channel needs most, and tell me what you assumed." },
    { label: "See how others do it", ask: "Find 3 to 5 examples of how others run projects like this one, and say what we should borrow." },
  ];
  return [
    { label: "Plan it", ask: `Make a short step-by-step plan for this goal: "${goal}". Say who should do each step and what you need from me.` },
    { label: "First draft", ask: `Make a first draft for this goal: "${goal}". Keep it short so I can react to it, and tell me what you assumed.` },
    { label: "See how others do it", ask: `Find 3 to 5 examples of how others have done this: "${goal}". Say what each does well and what we should borrow.` },
  ];
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
      <p>Ask for anything this channel needs; Lightbulb hands it to the right specialist.</p>
      {asks.length > 0 && <div className="room-intro-asks" role="group" aria-label="Suggested asks">
        {asks.map(({ label, ask }) => <Button key={label} type="button" variant="outline" size="sm" title={ask} onClick={() => onPickAsk!(ask)}>
          {label}</Button>)}
      </div>}
    </div>;
  }
  const title = kind === "dm" ? `This is the beginning of your direct messages with ${roomName}.`
    : kind === "channel" ? `This is the beginning of #${roomName}.`
      : `This is the beginning of ${roomName}.`;
  const detail = room?.shared ? "This channel is shared with the Lightbulb team — ask us anything here."
    : kind === "dm"
    ? (agents.length > 0 ? `${roomName} replies to everything you send here.` : "Messages here are private to the two of you.")
    : agents.length === 0 ? "Mention an agent to bring one in."
      : `${names.format(agents.map((agent) => agent.displayName))} ${agents.length === 1 ? "is" : "are"} here. Mention one when you want help.`;
  return <div className="room-intro"><h3>{title}</h3><p>{detail}</p></div>;
}
