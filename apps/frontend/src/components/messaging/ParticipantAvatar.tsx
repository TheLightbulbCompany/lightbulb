import { Terminal } from "lucide-react";
import { Identity, type IdentityProps } from "@/components/teams/shared/components/Identity";
import { LogoMark } from "@/components/Logo";
import { agentTint } from "@/lib/agentTint";
import type { DirectoryParticipant, RoomParticipant } from "./types";

const ACTIVITY_LABEL = {
  running: "Agent working",
  attention: "Agent needs attention",
  paused: "Agent paused",
} as const;

interface ParticipantAvatarProps {
  participant: DirectoryParticipant | RoomParticipant;
  size?: IdentityProps["size"];
  /** Off in stacked facepiles (the next face paints over the corner) and on
   *  message rows (Slack keeps those quiet); the dot and its sr-only text go together. */
  showPresence?: boolean;
}

export function ParticipantAvatar({ participant, size = "default", showPresence = true }: ParticipantAvatarProps) {
  const activity = participant.kind === "agent" ? participant.agentActivity : undefined;
  const label = activity && activity in ACTIVITY_LABEL
    ? ACTIVITY_LABEL[activity as keyof typeof ACTIVITY_LABEL]
    : undefined;
  const online = showPresence && participant.kind === "human" && participant.online === true;
  // Agents are always reachable, so they always show a presence dot (blue, distinct
  // from a human's green "online") -- never for offline humans.
  const isAgent = showPresence && participant.kind === "agent";
  const presence = online ? "online" : isAgent ? "agent" : undefined;
  const sharing = showPresence && participant.kind === "human" && participant.sharing === true;
  // Identity tint is keyed off kind, not showPresence: a face in a stacked
  // facepile hides its dot but is still the same agent, and losing the tint
  // there would make the pile unreadable — which is the whole problem the
  // tint exists to solve.
  // Lightbulb (the front agent, handle `lightbulb`) wears its mark, not "LI" initials (W13).
  const lightbulb = participant.kind === "agent" && participant.mentionHandle === "lightbulb";
  const tint = participant.kind === "agent" && !lightbulb ? agentTint(participant.id) : null;
  return <span className="participant-avatar" data-agent-state={label ? activity : undefined} data-presence={presence} data-sharing={sharing ? "true" : undefined}>
    <Identity name={participant.displayName} avatarOnly size={size} tint={tint} mark={lightbulb ? <LogoMark className="participant-avatar__mark" /> : undefined} />
    {label && <span className="sr-only">{label}</span>}
    {online && <span className="sr-only">Online</span>}
    {isAgent && <span className="sr-only">Agent, always available</span>}
    {sharing && <Terminal aria-hidden="true" className="participant-avatar-sharing" />}
    {sharing && <span className="sr-only">Sharing a terminal session</span>}
  </span>;
}
