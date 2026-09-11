import type { MemberRef } from "@lightbulb/shared";
import type { RoomParticipant } from "./types";

export const MENTION_SCHEME = "mention:";
type Target = Pick<RoomParticipant, "kind" | "id" | "displayName" | "mentionHandle">;

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export function parseMentionHref(href: string): { kind: "human" | "agent"; id: string } | null {
  if (!href.startsWith(MENTION_SCHEME)) return null;
  const [kind, ...rest] = href.slice(MENTION_SCHEME.length).split(":");
  const id = rest.join(":");
  return (kind === "human" || kind === "agent") && id ? { kind, id } : null;
}

export function linkifyMentions(
  text: string,
  // Unused: the composer already resolves typed @tokens to structured
  // mentions on send. This rewrite links any member token found in the text
  // regardless of whether it is also present here as a structured mention.
  mentions: readonly MemberRef[],
  participants: readonly Target[],
): string {
  const tokens = participants
    .map((participant) => ({ participant, token: participant.kind === "agent" ? participant.mentionHandle : participant.displayName }))
    .filter((entry): entry is { participant: Target; token: string } => !!entry.token)
    .sort((left, right) => right.token.length - left.token.length);
  if (tokens.length === 0) return text;
  const pattern = new RegExp(`(^|[\\s(])@(${tokens.map((entry) => escapeRegExp(entry.token)).join("|")})(?![\\w.-]|\\]\\()`, "gi");
  const rewrite = (segment: string) =>
    segment.replace(pattern, (match, lead: string, token: string) => {
      const entry = tokens.find((candidate) => candidate.token.toLowerCase() === token.toLowerCase());
      if (!entry) return match;
      return `${lead}[@${token}](${MENTION_SCHEME}${entry.participant.kind}:${entry.participant.id})`;
    });
  // Even segments are prose; odd segments are code (inline `…` or fenced ```…```).
  return text
    .split(/(```[\s\S]*?```|`[^`\n]*`)/)
    .map((segment, index) => (index % 2 === 0 ? rewrite(segment) : segment))
    .join("");
}
