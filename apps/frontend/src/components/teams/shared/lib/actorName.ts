import type { CompanyAgent, CompanyMember } from "../types";

/** Who made a change: the agent by id, else the member by Clerk or Paperclip user id ("You" for the current user), else "Someone". */
export function actorName({ agentId, userId, agents, members, currentUserId }: {
  agentId?: string | null;
  userId?: string | null;
  agents?: CompanyAgent[];
  members?: CompanyMember[];
  currentUserId?: string | null;
}): string {
  if (agentId) {
    const agent = agents?.find((a) => a.id === agentId);
    if (agent?.name) return agent.name;
  }
  if (userId) {
    const member = members?.find((m) => m.userId === userId || m.paperclipUserId === userId);
    if (member) return currentUserId && member.userId === currentUserId ? "You" : member.name ?? member.email ?? "Someone";
  }
  return "Someone";
}
