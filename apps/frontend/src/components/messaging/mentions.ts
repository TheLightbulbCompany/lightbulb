import type { MemberRef } from "@lightbulb/shared";
import type { RoomParticipant } from "./types";

export const MENTION_SCHEME = "mention:";
type Target = Pick<RoomParticipant, "kind" | "id" | "displayName" | "mentionHandle">;

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

interface TokenEntry {
  participant: Target;
  token: string;
}

// ONE grammar for both readers: what renders as a chip is exactly what
// resolves to a structured mention on send. Split them and a typed token can
// look mentioned and notify nobody.
// A human answers to their handle AND their display name: rows that predate
// server-side human handles carry mentionHandle: null and are still addressed
// by name.
function tokenEntries(participants: readonly Target[]): TokenEntry[] {
  return participants
    .flatMap((participant) =>
      [participant.mentionHandle, participant.kind === "human" ? participant.displayName : null]
        .map((token) => ({ participant, token })))
    .filter((entry): entry is TokenEntry => !!entry.token)
    .sort((left, right) => right.token.length - left.token.length);
}

const tokenPattern = (entries: readonly TokenEntry[]) =>
  new RegExp(`(^|[\\s(])@(${entries.map((entry) => escapeRegExp(entry.token)).join("|")})(?![\\w.-]|\\]\\()`, "gi");

// Even segments are prose; odd segments are code (inline `…` or fenced ```…```).
const proseSegments = (text: string) => text.split(/(```[\s\S]*?```|`[^`\n]*`)/);

export function parseMentionHref(href: string): { kind: "human" | "agent"; id: string } | null {
  if (!href.startsWith(MENTION_SCHEME)) return null;
  const [kind, ...rest] = href.slice(MENTION_SCHEME.length).split(":");
  const id = rest.join(":");
  return (kind === "human" || kind === "agent") && id ? { kind, id } : null;
}

export function linkifyMentions(
  text: string,
  // Unused: the composer resolves typed @tokens to structured mentions on
  // send. This rewrite links any member token found in the text regardless of
  // whether it is also present here as a structured mention.
  mentions: readonly MemberRef[],
  participants: readonly Target[],
): string {
  const entries = tokenEntries(participants);
  if (entries.length === 0) return text;
  const pattern = tokenPattern(entries);
  const rewrite = (segment: string) =>
    segment.replace(pattern, (match, lead: string, token: string) => {
      const entry = entries.find((candidate) => candidate.token.toLowerCase() === token.toLowerCase());
      if (!entry) return match;
      return `${lead}[@${token}](${MENTION_SCHEME}${entry.participant.kind}:${entry.participant.id})`;
    });
  return proseSegments(text)
    .map((segment, index) => (index % 2 === 0 ? rewrite(segment) : segment))
    .join("");
}

/** Every room member named by a plain `@token` in the prose of `text`. */
export function resolveTypedMentions(text: string, participants: readonly Target[]): MemberRef[] {
  const entries = tokenEntries(participants);
  if (entries.length === 0) return [];
  const pattern = tokenPattern(entries);
  const found: MemberRef[] = [];
  proseSegments(text).forEach((segment, index) => {
    if (index % 2 !== 0) return;
    for (const match of segment.matchAll(pattern)) {
      const entry = entries.find((candidate) => candidate.token.toLowerCase() === match[2].toLowerCase());
      if (!entry) continue;
      const ref = { kind: entry.participant.kind, id: entry.participant.id };
      if (!found.some((existing) => existing.kind === ref.kind && existing.id === ref.id)) found.push(ref);
    }
  });
  return found;
}
