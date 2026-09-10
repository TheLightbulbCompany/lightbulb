import { Identity, type IdentityProps } from "@/components/teams/shared/components/Identity";
import type { DirectoryParticipant, RoomParticipant } from "./types";

const ACTIVITY_LABEL = {
  running: "Agent working",
  attention: "Agent needs attention",
  paused: "Agent paused",
} as const;

interface ParticipantAvatarProps {
  participant: DirectoryParticipant | RoomParticipant;
  size?: IdentityProps["size"];
}

export function ParticipantAvatar({ participant, size = "default" }: ParticipantAvatarProps) {
  const activity = participant.kind === "agent" ? participant.agentActivity : undefined;
  const label = activity && activity in ACTIVITY_LABEL
    ? ACTIVITY_LABEL[activity as keyof typeof ACTIVITY_LABEL]
    : undefined;
  return <span className="participant-avatar" data-agent-state={label ? activity : undefined}>
    <Identity name={participant.displayName} avatarOnly size={size} />
    {label && <span className="sr-only">{label}</span>}
  </span>;
}
